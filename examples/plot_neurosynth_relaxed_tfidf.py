# -*- coding: utf-8 -*-
"""
Two-term conjunctive Coordinate-Based Meta-Analysis (CBMA) forward inference \
with relaxed TFIDF thresholding on the Neurosynth database
============================================================================

This example first shows how to encode the Neurosynth Coordinate-Based
Meta-Analysis (CBMA) database in NeuroLang, with probabilistic term-to-study
associations using a sigmoid-based soft thresholding of TFIDF features.

It then shows how a two-term conjunctive query can be expressed and solved to
obtain an uncorreted forward inference map for studies associated with both
terms 'auditory' and 'memory'.
"""
from typing import Iterable, Callable

import nibabel
import nilearn.datasets
import nilearn.image
import nilearn.plotting
import numpy as np
import pandas as pd

from neurolang.frontend import ExplicitVBR, ExplicitVBROverlay, NeurolangPDL

###############################################################################
# Data preparation
# ----------------

###############################################################################
# Load the MNI atlas and resample it to 4mm voxels

mni_t1 = nibabel.load(nilearn.datasets.fetch_icbm152_2009()["t1"])
mni_t1_4mm = nilearn.image.resample_img(mni_t1, np.eye(3) * 4)

###############################################################################
# Define a function that transforms TFIDF features to probabilities

term_1 = "memory"
term_2 = "auditory"
terms = [term_1, term_2]

###############################################################################
# Probabilistic Logic Programming in NeuroLang
# --------------------------------------------

nl = NeurolangPDL()

###############################################################################
# Adding new aggregation function to build a region overlay


@nl.add_symbol
def agg_create_region_overlay(
    i: Iterable, j: Iterable, k: Iterable, p: Iterable
) -> ExplicitVBR:
    voxels = np.c_[i, j, k]
    return ExplicitVBROverlay(
        voxels, mni_t1_4mm.affine, p, image_dim=mni_t1_4mm.shape
    )


###############################################################################
# Loading the database

ns_database_fn, ns_features_fn = nilearn.datasets.utils._fetch_files(
    "neurolang",
    [
        (
            "database.txt",
            "https://github.com/neurosynth/neurosynth-data"
            "/raw/master/current_data.tar.gz",
            {"uncompress": True},
        ),
        (
            "features.txt",
            "https://github.com/neurosynth/neurosynth-data"
            "/raw/master/current_data.tar.gz",
            {"uncompress": True},
        ),
    ],
)

ns_database = pd.read_csv(ns_database_fn, sep="\t")
ijk_positions = np.round(
    nibabel.affines.apply_affine(
        np.linalg.inv(mni_t1_4mm.affine),
        ns_database[["x", "y", "z"]].values.astype(float),
    )
).astype(int)
ns_database["i"] = ijk_positions[:, 0]
ns_database["j"] = ijk_positions[:, 1]
ns_database["k"] = ijk_positions[:, 2]
ns_database = set(
    ns_database[["i", "j", "k", "id"]].itertuples(name=None, index=False)
)

ns_features = pd.read_csv(ns_features_fn, sep="\t")
ns_docs = ns_features[["pmid"]].drop_duplicates().values
ns_tfidf = pd.melt(
    ns_features, var_name="term", id_vars="pmid", value_name="TfIdf"
)
ns_tfidf = ns_tfidf[["pmid", "term", "TfIdf"]].values

StudyTFIDF = nl.add_tuple_set(ns_tfidf, name="StudyTFIDF")
VoxelReported = nl.add_tuple_set(ns_database, name="VoxelReported")
SelectedStudy = nl.add_uniform_probabilistic_choice_over_set(
    ns_docs, name="SelectedStudy"
)

###############################################################################
# Probabilistic program and querying

nl.add_symbol(np.exp, name="exp", type_=Callable[[float], float])

with nl.environment as e:
    (e.TermInStudy @ (1 / (1 + e.exp(-e.alpha * (e.tfidf - e.tau)) + 1)))[
        e.t, e.s
    ] = (e.StudyTFIDF[e.s, e.t, e.tfidf] & (e.alpha == 3000) & (e.tau == 0.01))
    e.TermAssociation[e.t] = e.SelectedStudy[e.s] & e.TermInStudy[e.t, e.s]
    e.Activation[e.i, e.j, e.k] = (
        e.SelectedStudy[e.s] & e.VoxelReported[e.i, e.j, e.k, e.s]
    )
    e.probmap[e.i, e.j, e.k, e.PROB[e.i, e.j, e.k]] = (
        e.Activation[e.i, e.j, e.k]
    ) // (e.TermAssociation["auditory"] & e.TermAssociation["memory"])
    e.img[e.agg_create_region_overlay[e.i, e.j, e.k, e.p]] = e.probmap[
        e.i, e.j, e.k, e.p
    ]
    img_query = nl.query((e.x,), e.img[e.x])

###############################################################################
# Plotting results
# --------------------------------------------

result_image = img_query.fetch_one()[0].spatial_image()
img = result_image.get_fdata()
plot = nilearn.plotting.plot_stat_map(
    result_image, threshold=np.percentile(img[img > 0], 95)
)
nilearn.plotting.show()
