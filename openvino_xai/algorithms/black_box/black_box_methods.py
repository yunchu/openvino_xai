# mypy: disable-error-code="union-attr"

# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from abc import ABC
from typing import Optional, Tuple, List

import cv2
import numpy as np
from tqdm import tqdm

from openvino_xai.explanation.explanation_parameters import ExplanationParameters


class BlackBoxXAIMethodBase(ABC):
    """Base class for methods that explain model in Black-Box mode."""


class RISE(BlackBoxXAIMethodBase):
    """RISEExplainer explains classification models in black-box mode using RISE (https://arxiv.org/abs/1806.07421).

    :param model_inferrer: Callable model inferrer object.
    :type model_inferrer: Union[Callable[[np.ndarray], InferenceResult], mapi_models.Model]
    :param num_masks: Number of generated masks to aggregate.
    :type num_masks: int
    :param num_cells: Number of cells for low-dimensional RISE
        random mask that later will be up-scaled to the model input size.
    :type num_cells: int
    :param prob: With prob p, a low-res cell is set to 1;
        otherwise, it's 0. Default: ``0.5``.
    :type prob: float
    :param seed: Seed for random mask generation.
    :type seed: int
    :param input_size: Model input size.
    :type input_size: Tuple[int]
    :param asynchronous_inference: Whether to run inference in asynchronous mode or not.
    :type asynchronous_inference: bool
    :param throughput_inference: Whether to run asynchronous inference in throughput mode or not.
    :type throughput_inference: bool
    :param normalize: Whether to normalize output or not.
    :type normalize: bool
    """

    @classmethod
    def run(
            cls,
            compiled_model,
            preprocess_fn,
            postprocess_fn,
            data: np.ndarray,
            explanation_parameters: ExplanationParameters = ExplanationParameters(),
            num_masks: int = 5000,
            num_cells: int = 8,
            prob: float = 0.5,
            seed: int = 0,
            normalize: bool = True,
    ):
        """Generates inference result of RISE algorithm."""
        data_preprocessed = preprocess_fn(data)

        saliency_maps = cls._run_synchronous_explanation(
            data_preprocessed,
            explanation_parameters.target_explain_indices,
            compiled_model,
            postprocess_fn,
            num_masks,
            num_cells,
            prob,
            seed,
        )

        if normalize:
            saliency_maps = cls._normalize_saliency_maps(saliency_maps)
        saliency_maps = np.expand_dims(saliency_maps, axis=0)
        return saliency_maps

    @classmethod
    def _run_synchronous_explanation(
            cls,
            data_preprocessed: np.ndarray,
            target_classes: Optional[List[int]],
            compiled_model,
            postprocess_fn,
            num_masks,
            num_cells,
            prob,
            seed,
    ) -> np.ndarray:
        _, _, height, width = data_preprocessed.shape
        input_size = height, width

        forward_output = compiled_model(data_preprocessed)
        logits = postprocess_fn(forward_output)
        _, num_classes = logits.shape

        if target_classes is None:
            num_targets = num_classes
        else:
            num_targets = len(target_classes)

        rand_generator = np.random.default_rng(seed=seed)

        sal_maps = np.zeros((num_targets, input_size[0], input_size[1]))
        for _ in tqdm(range(0, num_masks), desc="Explaining in synchronous mode"):
            mask = cls._generate_mask(input_size, num_cells, prob, rand_generator)
            # Add channel dimensions for masks
            masked = mask * data_preprocessed

            forward_output = compiled_model(masked)
            raw_scores = postprocess_fn(forward_output)

            sal = cls._get_scored_mask(raw_scores, mask, target_classes)
            sal_maps += sal

        if target_classes is not None:
            sal_maps = cls._reconstruct_sparce_saliency_map(sal_maps, num_classes, input_size, target_classes)
        return sal_maps

    @staticmethod
    def _get_scored_mask(raw_scores: np.ndarray, mask: np.ndarray, target_classes: Optional[List[int]]) -> np.ndarray:
        if target_classes:
            return np.take(raw_scores, target_classes).reshape(-1, 1, 1) * mask
        else:
            return raw_scores.reshape(-1, 1, 1) * mask

    @staticmethod
    def _reconstruct_sparce_saliency_map(
        sal_maps: np.ndarray, num_classes: int, input_size, target_classes: Optional[List[int]]
    ) -> np.ndarray:
        # TODO: see if np.put() or other alternatives works faster (requires flatten array)
        sal_maps_tmp = sal_maps
        sal_maps = np.zeros((num_classes, input_size[0], input_size[1]))
        for i, sal in enumerate(sal_maps_tmp):
            sal_maps[target_classes[i]] = sal
        return sal_maps

    @staticmethod
    def _generate_mask(input_size: Tuple[int, int], num_cells: int, prob: float, rand_generator) -> np.ndarray:
        """Generate masks for RISE
        Returns:
            mask (np.array): float mask from 0 to 1 with size of model input

        """
        cell_size = np.ceil(np.array(input_size) / num_cells)
        up_size = np.array((num_cells + 1) * cell_size, dtype=np.uint32)

        grid_size = (num_cells, num_cells)
        grid = rand_generator.random(grid_size) < prob
        grid = grid.astype(np.float32)

        # Random shifts
        x = rand_generator.integers(0, cell_size[0])
        y = rand_generator.integers(0, cell_size[1])
        # Linear up-sampling and cropping
        upsampled_mask = cv2.resize(grid, up_size, interpolation=cv2.INTER_CUBIC)
        mask = upsampled_mask[x : x + input_size[0], y : y + input_size[1]]
        mask = np.clip(mask, 0, 1)
        return mask

    @staticmethod
    def _normalize_saliency_maps(saliency_map: np.ndarray) -> np.ndarray:
        n, h, w = saliency_map.shape
        saliency_map = saliency_map.reshape((n, h * w))
        min_values = np.min(saliency_map, axis=-1)
        max_values = np.max(saliency_map, axis=-1)
        saliency_map = 255 * (saliency_map - min_values[:, None]) / (max_values - min_values + 1e-12)[:, None]
        saliency_map = saliency_map.reshape((n, h, w)).astype(np.uint8)
        return saliency_map
