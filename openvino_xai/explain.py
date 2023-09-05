# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from abc import ABC
from abc import abstractmethod
from typing import Dict, Any, Optional, List

import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm
import openvino

from openvino_xai.model import XAIModel, XAIClassificationModel
from openvino_xai.parameters import ExplainParameters, PostProcessParameters
from openvino_xai.saliency_map import ClassificationResult, ExplainResult, PostProcessor, TargetExplainGroup
from openvino_xai.utils import logger


class Explainer(ABC):
    """A base interface for explainer."""

    def __init__(self, model: openvino.model_api.models.Model):
        self._model = model
        self._explain_method = self._model.explain_method if hasattr(self._model, "explain_method") else None
        self._labels = self._model.labels

    @abstractmethod
    def explain(self, data: np.ndarray) -> ExplainResult:
        """Explain the input."""
        # TODO: handle path_to_data as input as well?
        raise NotImplementedError

    def _get_target_explain_group(self, target_explain_group):
        if target_explain_group:
            if self._explain_method:
                assert target_explain_group in self._explain_method.supported_target_explain_groups, \
                    f"Provided target_explain_group {target_explain_group} is not supported by the explain method."
            return target_explain_group
        else:
            if self._explain_method:
                return self._explain_method.default_target_explain_group
            else:
                raise ValueError("Model with XAI branch was created outside of Openvino-XAI library. "
                                 "Please explicitly provide target_explain_group to the explain call.")

    @staticmethod
    def _get_processed_explain_result(raw_explain_result, data, post_processing_parameters):
        post_processor = PostProcessor(
            raw_explain_result,
            data,
            post_processing_parameters,
        )
        processed_explain_result = post_processor.postprocess()
        return processed_explain_result

class WhiteBoxExplainer(Explainer):
    """Explainer explains models with XAI branch injected."""

    def explain(
            self,
            data: np.ndarray,
            target_explain_group: Optional[TargetExplainGroup] = None,
            explain_targets: Optional[List[int]] = None,
            post_processing_parameters: PostProcessParameters = PostProcessParameters(),
    ) -> ExplainResult:
        """Explain the input in white box mode.

        :param data: Data to explain.
        :type data: np.ndarray
        :param target_explain_group: Defines targets to explain: all classes, only predicted classes, etc.
        :type target_explain_group: TargetExplainGroup
        :param explain_targets: Provides list of custom targets, optional.
        :type explain_targets: Optional[List[int]]
        :param post_processing_parameters: Parameters that define post-processing.
        :type post_processing_parameters: PostProcessParameters
        """
        raw_result = self._model(data)

        target_explain_group = self._get_target_explain_group(target_explain_group)
        raw_explain_result = ExplainResult(raw_result, target_explain_group, explain_targets, self._labels)

        processed_explain_result = self._get_processed_explain_result(
            raw_explain_result, data, post_processing_parameters
        )
        return processed_explain_result


class BlackBoxExplainer(Explainer):
    """Base class for explainers that consider model as a black-box."""


class RISEExplainer(BlackBoxExplainer):
    def __init__(self, model, num_masks=5000, num_cells=8, prob=0.5):
        """RISE BlackBox Explainer

        Args:
            num_masks (int, optional): number of generated masks to aggregate
            num_cells (int, optional): number of cells for low-dimensional RISE
                random mask that later will be upscaled to the model input size
            prob (float, optional): with prob p, a low-res cell is set to 1;
                otherwise, it's 0. Default: ``0.5``.

        """
        super().__init__(model)
        self.input_size = model.inputs["data"].shape[-2:]
        self.num_masks = num_masks
        self.num_cells = num_cells
        self.prob = prob

    def explain(
        self,
        data,
        target_explain_group: Optional[TargetExplainGroup] = None,
        explain_targets: Optional[List[int]] = None,
        post_processing_parameters: Optional[Dict[str, Any]] = None,
    ):
        """Explain the input."""
        raw_saliency_map = self._generate_saliency_map(data)

        resized_data = self._resize_input(data)
        predicted_classes = self._model(resized_data)[0]
        cls_result = ClassificationResult(predicted_classes, raw_saliency_map, np.ndarray(0), np.ndarray(0))
        
        target_explain_group = self._get_target_explain_group(target_explain_group)
        explain_result = ExplainResult(cls_result, target_explain_group, explain_targets, self._labels)

        processed_explain_result = self._get_processed_explain_result(
            explain_result, data, post_processing_parameters
        )

        return processed_explain_result

    def _generate_saliency_map(self, data):
        """Generate RISE saliency map
        Returns:
            sal (np.ndarray): saliency map for each class

        """
        cell_size = np.ceil(np.array(self.input_size) / self.num_cells)
        up_size = np.array((self.num_cells + 1) * cell_size, dtype=np.uint32)
        rand_generator = np.random.default_rng(seed=42)

        resized_data = self._resize_input(data)

        sal_maps = []
        for i in tqdm(range(0, self.num_masks), desc="Explaining"):
            mask = self._generate_mask(cell_size, up_size, rand_generator)
            # Add channel dimentions for masks
            masked = np.expand_dims(mask, axis=2) * resized_data
            scores = self._model(masked).raw_scores
            sal = scores.reshape(-1, 1, 1) * mask
            sal_maps.append(sal)
        sal_maps = np.sum(sal_maps, axis=0)

        sal_maps = self._normalize_saliency_maps(sal_maps)
        sal_maps = np.expand_dims(sal_maps, axis=0)
        return sal_maps

    def _generate_mask(self, cell_size, up_size, rand_generator):
        """Generate masks for RISE
            cell_size (int): calculated size of one cell for low-dimensional RISE
            up_size (int): increased cell size to crop
            rand_generator (np.random.generator): generator with fixed seed to generate random masks  
        Returns:
            masks (np.array): self.num_masks float masks from 0 to 1 with size of input model

        """
        grid_size = (self.num_cells, self.num_cells)
        grid = rand_generator.random(grid_size) < self.prob
        grid = grid.astype(np.float32)

        # Random shifts
        x = rand_generator.integers(0, cell_size[0])
        y = rand_generator.integers(0, cell_size[1])
        # Linear upsampling and cropping
        upsampled_mask = cv2.resize(grid, up_size, interpolation=cv2.INTER_LINEAR)
        mask = upsampled_mask[x : x + self.input_size[0], y : y + self.input_size[1]]

        return mask

    def _resize_input(self, image):
        image = cv2.resize(image, self.input_size, Image.BILINEAR)
        return image

    def _normalize_saliency_maps(self, saliency_map):
        min_values = np.min(saliency_map)
        max_values = np.max(saliency_map)
        saliency_map = 255 * (saliency_map - min_values) / (max_values - min_values + 1e-12)
        return saliency_map


class DRISEExplainer(BlackBoxExplainer):
    def explain(self, data: np.ndarray) -> ExplainResult:
        """Explain the input."""
        raise NotImplementedError


class AutoExplainer(Explainer):
    """Explain in auto mode, using white box or black box approach."""

    def __init__(self, model: openvino.model_api.models.Model, explain_parameters: Optional[ExplainParameters] = None):
        super().__init__(model)
        self._explain_parameters = explain_parameters


class ClassificationAutoExplainer(AutoExplainer):
    """Explain classification models in auto mode, using white box or black box approach."""

    def explain(self, data: np.ndarray, target_explain_group: Optional[TargetExplainGroup] = None) -> ExplainResult:
        """
        Implements three explain scenarios, for different IR models:
            1. IR model contain xai branch -> infer Model API wrapper.
            2. If not (1), IR model can be augmented with XAI branch -> augment and infer.
            3. If not (1) and (2), IR model can NOT be augmented with XAI branch -> use XAI BB method.

        :param data: Data to explain.
        :type data: np.ndarray
        :param target_explain_group: Target explain group.
        :type target_explain_group: TargetExplainGroup
        """
        if XAIModel.has_xai(self._model.inference_adapter.model):
            logger.info("Model already has XAI - using White Box explainer.")
            explanations = WhiteBoxExplainer(self._model).explain(data, target_explain_group)
            return explanations
        else:
            try:
                logger.info("Model does not have XAI - trying to insert XAI and use White Box explainer.")
                self._model = XAIClassificationModel.insert_xai(self._model, self._explain_parameters)
                explanations = WhiteBoxExplainer(self._model).explain(data)
                return explanations
            except Exception as e:
                print(e)
                logger.info("Failed to insert XAI into the model. Calling Black Box explainer.")
                explanations = RISEExplainer(self._model).explain(data)
                return explanations


class DetectionAutoExplainer(AutoExplainer):
    """Explain detection models in auto mode, using white box or black box approach."""

    def explain(self, data: np.ndarray) -> np.ndarray:
        """Explain the input."""
        raise NotImplementedError
