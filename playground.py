import itertools
from typing import List, Sequence

import numpy as np
import scipy
import tqdm
from scipy.sparse import dok_matrix, lil_matrix
from scipy.sparse.linalg import lsqr
from scipy.spatial import KDTree

import meshlib
from render import get_markers, BrowserVisualizer


original_source = meshlib.Mesh.from_file_obj("models/lowpoly_cat/cat_reference.obj")
original_target = meshlib.Mesh.from_file_obj("models/lowpoly_dog/dog_reference.obj")
markers = get_markers()  # cat, dog
# markers = np.transpose((markers[:, 0], markers[:, 0]))

target_mesh = original_target.to_fourth_dimension()
subject = original_source.to_fourth_dimension()
# Show the source and target
# MeshPlots.side_by_side([original_source, original_target]).show(renderer="browser")

# Weights of cost functions
Ws = 1.0
Wi = 0.001
Wc = [1.0, 200.0, 1000.0, 5000.0]

# Precalculate the adjacent triangles in source
print("Prepare adjacent list")

# def is_adjacent_edge(a: np.ndarray, b: np.ndarray):
#     return any(
#         (a[list(perm)] == b).sum() == 2 for perm in itertools.permutations((0, 1, 2), 3)
#     )
#
# adjacent_edges: List[List[int]] = [
#     [j for j, o in enumerate(original_source.faces) if i != j and is_adjacent_edge(o, f)]
#     for i, f in enumerate(original_source.faces)
# ]

adjacent_vertices: List[List[int]] = [
    list(set(j
             for perm in ((0, 1, 2), (2, 0, 1), (1, 2, 0))
             for j in np.where(original_source.faces == f[list(perm)])[0]
             if j != i))
    for i, f in enumerate(original_source.faces)]

adjacent = adjacent_vertices


#
# for i, f in enumerate(original_source.faces):
#     set(*(np.where(original_source.faces == f[perm]) for perm in np.itertools.permutations(range(3), 3)))


def get_closest_points(kd_tree: KDTree, verts: np.array):
    return kd_tree.query(verts)[1]


def get_aec(num_verts):
    return scipy.sparse.identity(num_verts*3, dtype=np.float, format="lil")


def get_bec(closest_points: np.array, verts: np.array):
    return verts[closest_points]


class TransformEntry:
    """
    Class for creating the transformation matrix solution for T=xV^-1
    """

    def __init__(self, face: np.ndarray, invV: np.ndarray):
        assert face.shape == (4,)
        assert invV.shape == (3, 3)
        self.face = face
        """
        Solving
        x = [v2-v1, v3-v1, v4-v1]^-1
        w = xV^{-1}
        w_ij = v1 x2 - v1 x1 + v2 x3 - v2 x1 + v3 x4 - v3 x1
        w_ij = -(v1+v2+v3) x1 + (v1) x2 + (v2) x3 + (v3) x4
        """
        self.kleinA = np.zeros(shape=(9, 12))
        # Build T = V~ V^-1
        for i in range(3):  # Row of T
            for j in range(3):  # Column of T
                r = 3 * j + i
                self.kleinA[r, i] = - (invV[0, j] + invV[1, j] + invV[2, j])
                self.kleinA[r, i + 3] = invV[0, j]
                self.kleinA[r, i + 6] = invV[1, j]
                self.kleinA[r, i + 9] = invV[2, j]

    def insert_to(self, target, row: int, factor=1.0):
        # Index
        i0 = self.face[0] * 3
        i1 = self.face[1] * 3
        i2 = self.face[2] * 3
        i3 = self.face[3] * 3
        # Insert by adding
        part = self.kleinA * factor
        target[row:row + 9, i0:i0 + 3] += part[:, 0:3]
        target[row:row + 9, i1:i1 + 3] += part[:, 3:6]
        target[row:row + 9, i2:i2 + 3] += part[:, 6:9]
        target[row:row + 9, i3:i3 + 3] += part[:, 9:12]

    #########################################################
    # Create inverse of triangle spans


#########################################################
print("Inverse Triangle Spans")
invVs = np.linalg.inv(subject.span)
assert len(subject.faces) == len(invVs)

#########################################################
# Preparing the transformation matrices
print("Preparing Transforms")
transforms = [TransformEntry(f, invV) for f, invV in zip(subject.faces, invVs)]

#########################################################
# Identity Cost - of transformations
Bi = np.tile(np.identity(3, dtype=np.float).flatten(), len(subject.faces))
AEi = lil_matrix(
    (
        # Count of all minimization terms
        len(subject.faces) * 9,
        # Length of flat result x
        len(subject.vertices) * 3
    ),
    dtype=np.float
)
assert AEi.shape[0] == len(Bi)
for index, Ti in enumerate(tqdm.tqdm(transforms, desc="Building Identity Cost")):  # type: int, TransformEntry
    Ti.insert_to(AEi, row=index * 9)

#########################################################
# Smoothness Cost - of differences to adjacent transformations
count_adjacent = sum(len(a) for a in adjacent)
Bs = np.zeros(count_adjacent * 9)
AEs = lil_matrix(
    (
        # Count of all minimization terms
        count_adjacent * 9,
        # Length of flat result x
        len(subject.vertices) * 3
    ),
    dtype=np.float
)
assert AEs.shape[0] == len(Bs)
row = 0
for index, Ti in enumerate(tqdm.tqdm(transforms, desc="Building Smoothness Cost")):  # type: int, TransformEntry
    for adj in adjacent[index]:
        Ti.insert_to(AEs, row)
        transforms[adj].insert_to(AEs, row, -1.0)
        row += 9
assert row == AEs.shape[0]

# KDTree for closest points in E_c
kd_tree_target = KDTree(target_mesh.vertices)
result_verts = 0

#########################################################
# Start of loop

iterations = 4
total_steps = 5  # Steps per iteration
# Progress bar
pBar = tqdm.tqdm(total=iterations * total_steps)

for iteration in range(iterations):

    def pbar_next(msg: str):
        pBar.set_description(f"[{iteration + 1}/{iterations}] {msg}")
        pBar.update()


    #########################################################
    pbar_next("Combining Costs")
    A = scipy.sparse.vstack((AEi * Wi, AEs * Ws), format="lil")
    b = np.concatenate((Bi * Wi, Bs * Ws))

    if iteration > 0:
        A = scipy.sparse.vstack([A, get_aec(len(subject.vertices)) * Wc[iteration]], format="lil")
        b = np.concatenate((b, get_bec(get_closest_points(kd_tree_target, result_verts.reshape((-1, 3))), target_mesh.vertices).flatten() * Wc[iteration]))

    #########################################################
    pbar_next("Enforcing Markers")
    for mark_src_i, mark_dest_i in markers:
        i = mark_src_i * 3
        valueB = A[:, i:i + 3] @ target_mesh.vertices[mark_dest_i]
        b -= valueB
        A[:, i:i + 3] = 0

    #########################################################
    pbar_next("Solving")
    # U, S, Vt = svds(A)
    # psInv = Vt.T @ np.linalg.inv(np.diag(S)) @ U.T
    # result = psInv @ b

    lsqr_result = lsqr(A, b)
    # lsqr_result = lsqr(A.T @ A, A.T @ b)
    result_verts = lsqr_result[0]

    #########################################################
    # Apply new vertices
    pbar_next("Applying vertices")
    vertices = result_verts.reshape((-1, 3))[:len(original_source.vertices)]
    result = meshlib.Mesh(vertices=vertices, faces=original_source.faces).to_fourth_dimension()
    # Enforce target vertices
    for mark_src_i, mark_dest_i in markers:
        result.vertices[mark_src_i] = target_mesh.vertices[mark_dest_i]

    #########################################################
    pbar_next("Rendering")

    vis = BrowserVisualizer()
    vis.add_mesh(result,
                 name=f"Result {iteration}",
                 text=[f"<b>Vertex:</b> {n}" for n in range(len(original_target.vertices))]
                 )
    vis.add_mesh(original_source,
                 name="Source",
                 color="red",
                 opacity=0.025,
                 # text=[f"<b>Vertex:</b> {n}" for n in range(len(original_target.vertices))]
                 hoverinfo='skip',
                 )
    vis.add_mesh(original_target,
                 name="Target",
                 color="blue",
                 opacity=0.025,
                 # text=[f"<b>Vertex:</b> {n}" for n in range(len(original_target.vertices))]
                 hoverinfo='skip',
                 )
    vis.add_scatter(
        original_target.vertices[markers[:, 1]],
        marker=dict(
            color='yellow',
            size=3,
            opacity=0.9,
            symbol='x',
        ),
        text=[f"<b>Index:</b> {t}" for s, t in markers],
        name="Marker Target"
    )
    vis.add_scatter(
        original_source.vertices[markers[:, 0]],
        marker=dict(
            color='red',
            size=3,
            opacity=0.9,
            symbol='x',
        ),
        text=[f"<b>Index:</b> {s}" for s, t in markers],
        name="Marker Source"
    )
    vis.add_scatter(
        original_target.vertices,
        marker=dict(
            color='blue',
            size=1,
            opacity=0.2,
        ),
        name="Vertex Target"
    )
    vis.show(renderer="browser")
