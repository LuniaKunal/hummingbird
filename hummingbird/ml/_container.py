# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

"""
All custom model containers are listed here.
In Hummingbird we use two types of containers:
- containers for input models (e.g., `CommonONNXModelContainer`) used to represent input models in a unified way as DAG of containers
- containers for output models (e.g., `SklearnContainer`) used to surface output models as unified API format.
"""

from abc import ABC, abstractmethod
import dill
import os
import numpy as np
from onnxconverter_common.container import CommonSklearnModelContainer
import shutil
import torch

from hummingbird.ml.operator_converters import constants
from hummingbird.ml._utils import onnx_runtime_installed, tvm_installed, pandas_installed, get_device, from_strings_to_ints

if pandas_installed():
    from pandas import DataFrame
else:
    DataFrame = None


# Input containers
class CommonONNXModelContainer(CommonSklearnModelContainer):
    """
    Common container for input ONNX operators.
    """

    def __init__(self, onnx_model):
        super(CommonONNXModelContainer, self).__init__(onnx_model)


class CommonSparkMLModelContainer(CommonSklearnModelContainer):
    """
    Common container for input Spark-ML operators.
    """

    def __init__(self, sparkml_model):
        super(CommonSparkMLModelContainer, self).__init__(sparkml_model)


# Output containers.
# Abstract containers enabling the Sklearn API.
class SklearnContainer(ABC):
    def __init__(self, model, n_threads=None, batch_size=None, extra_config={}):
        """
        Base container abstract class allowing to mirror the Sklearn API.
        *SklearnContainer* enables the use of `predict`, `predict_proba` etc. API of Sklearn
        also over the models generated by Hummingbird (irrespective of the selected backend).

        Args:
            model: Any Hummingbird supported model
            n_threads: How many threads should be used by the containter to run the model. None means use all threads.
            batch_size: If different than None, split the input into batch_size partitions and score one partition at a time.
            extra_config: Some additional configuration parameter.
        """
        self._model = model
        self._n_threads = n_threads
        self._extra_config = extra_config
        self._batch_size = batch_size

    @property
    def model(self):
        return self._model

    @abstractmethod
    def save(self, location):
        """
        Method used to save the container for future use.

        Args:
            location: The location on the file system where to save the model.
        """
        return

    def _run(self, function, *inputs):
        """
        This function scores the full dataset at once. See BatchContainer below for batched scoring.
        """
        if DataFrame is not None and type(inputs[0]) == DataFrame:
            # Split the dataframe into column ndarrays.
            inputs = inputs[0]
            input_names = list(inputs.columns)
            splits = [inputs[input_names[idx]] for idx in range(len(input_names))]
            inputs = [df.to_numpy().reshape(-1, 1) for df in splits]

        return function(*inputs)


class BatchContainer:
    def __init__(self, base_container, remainder_model_container=None):
        """
        A wrapper around one or two containers to do batch by batch prediction. The batch size is
        fixed when `base_container` is created. Together with `remainder_model_container`, this class
        enables prediction on a dataset of size `base_container._batch_size` * k +
        `remainder_model_container._batch_size`, where k is any integer. Its `predict` related method
        optionally takes `concatenate_outputs` argument, which when set to True causes the outputs to
        be returned as a list of individual prediction. This avoids an extra allocation of an output array
        and copying of each batch prediction into it.

        Args:
            base_container: One of subclasses of `SklearnContainer`.
            remainder_model_container: An auxiliary container that is used in the last iteration,
            if the test input batch size is not devisible by `base_container._batch_size`.
        """
        assert base_container._batch_size is not None
        self._base_container = base_container
        self._batch_size = base_container._batch_size

        if remainder_model_container:
            assert remainder_model_container._batch_size is not None
            self._remainder_model_container = remainder_model_container
            self._remainder_size = remainder_model_container._batch_size
        else:
            # This is remainder_size == 0 case
            # We repurpose base_container as a remainder_model_container
            self._remainder_model_container = base_container
            self._remainder_size = base_container._batch_size

    def __getattr__(self, name):
        return getattr(self._base_container, name)

    def decision_function(self, *inputs, concatenate_outputs=True):
        return self._predict_common(
            self._base_container.decision_function,
            self._remainder_model_container.decision_function,
            *inputs,
            concatenate_outputs=concatenate_outputs
        )

    def transform(self, *inputs, concatenate_outputs=True):
        return self._predict_common(
            self._base_container.transform,
            self._remainder_model_container.transform,
            *inputs,
            concatenate_outputs=concatenate_outputs
        )

    def score_samples(self, *inputs, concatenate_outputs=True):
        return self._predict_common(
            self._base_container.score_samples,
            self._remainder_model_container.score_samples,
            *inputs,
            concatenate_outputs=concatenate_outputs
        )

    def predict(self, *inputs, concatenate_outputs=True):
        return self._predict_common(
            self._base_container.predict,
            self._remainder_model_container.predict,
            *inputs,
            concatenate_outputs=concatenate_outputs
        )

    def predict_proba(self, *inputs, concatenate_outputs=True):
        return self._predict_common(
            self._base_container.predict_proba,
            self._remainder_model_container.predict_proba,
            *inputs,
            concatenate_outputs=concatenate_outputs
        )

    def _predict_common(self, predict_func, remainder_predict_func, *inputs, concatenate_outputs=True):
        if DataFrame is not None and type(inputs[0]) == DataFrame:
            # Split the dataframe into column ndarrays.
            inputs = inputs[0]
            input_names = list(inputs.columns)
            splits = [inputs[input_names[idx]] for idx in range(len(input_names))]
            inputs = tuple([df.to_numpy().reshape(-1, 1) for df in splits])

        def output_proc(predictions):
            if concatenate_outputs:
                return np.concatenate(predictions)
            return predictions

        is_tuple = isinstance(inputs, tuple)

        if is_tuple:
            total_size = inputs[0].shape[0]
        else:
            total_size = inputs.shape[0]

        if total_size == self._batch_size:
            # A single batch inference case
            return output_proc([predict_func(*inputs)])

        iterations = total_size // self._batch_size
        iterations += 1 if total_size % self._batch_size > 0 else 0
        iterations = max(1, iterations)
        predictions = []

        for i in range(0, iterations):
            start = i * self._batch_size
            end = min(start + self._batch_size, total_size)
            if is_tuple:
                batch = tuple([input[start:end, :] for input in inputs])
            else:
                batch = inputs[start:end, :]

            if i == iterations - 1:
                assert (end - start) == self._remainder_size
                out = remainder_predict_func(*batch)
            else:
                out = predict_func(*batch)

            predictions.append(out)

        return output_proc(predictions)


class SklearnContainerTransformer(SklearnContainer):
    """
    Abstract container mirroring Sklearn transformers API.
    """

    @abstractmethod
    def _transform(self, *input):
        """
        This method contains container-specific implementation of transform.
        """
        pass

    def transform(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On data transformers it returns transformed output data
        """
        return self._run(self._transform, *inputs)


class SklearnContainerRegression(SklearnContainer):
    """
    Abstract container mirroring Sklearn regressors API.
    """

    def __init__(
        self, model, n_threads, batch_size, is_regression=True, is_anomaly_detection=False, extra_config={}, **kwargs
    ):
        super(SklearnContainerRegression, self).__init__(model, n_threads, batch_size, extra_config)

        assert not (is_regression and is_anomaly_detection)

        self._is_regression = is_regression
        self._is_anomaly_detection = is_anomaly_detection

    @abstractmethod
    def _predict(self, *input):
        """
        This method contains container-specific implementation of predict.
        """
        pass

    def predict(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On regression returns the predicted values.
        On classification tasks returns the predicted class labels for the input data.
        On anomaly detection (e.g. isolation forest) returns the predicted classes (-1 or 1).
        """
        return self._run(self._predict, *inputs)


class SklearnContainerClassification(SklearnContainerRegression):
    """
    Container mirroring Sklearn classifiers API.
    """

    def __init__(self, model, n_threads, batch_size, extra_config={}):
        super(SklearnContainerClassification, self).__init__(
            model, n_threads, batch_size, is_regression=False, extra_config=extra_config
        )

    @abstractmethod
    def _predict_proba(self, *input):
        """
        This method contains container-specific implementation of predict_proba.
        """
        pass

    def predict_proba(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On classification tasks returns the probability estimates.
        """
        return self._run(self._predict_proba, *inputs)


class SklearnContainerAnomalyDetection(SklearnContainerRegression):
    """
    Container mirroring Sklearn anomaly detection API.
    """

    def __init__(self, model, n_threads, batch_size, extra_config={}):
        super(SklearnContainerAnomalyDetection, self).__init__(
            model, n_threads, batch_size, is_regression=False, is_anomaly_detection=True, extra_config=extra_config
        )

    @abstractmethod
    def _decision_function(self, *inputs):
        """
        This method contains container-specific implementation of decision_function.
        """
        pass

    def decision_function(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision function scores.
        """
        scores = self._run(self._decision_function, *inputs)

        # Backward compatibility for sklearn <= 0.21
        if constants.IFOREST_THRESHOLD in self._extra_config:
            scores += self._extra_config[constants.IFOREST_THRESHOLD]
        return scores

    def score_samples(self, *inputs):
        """
        Utility functions used to emulate the behavior of the Sklearn API.
        On anomaly detection (e.g. isolation forest) returns the decision_function score plus offset_
        """
        return self.decision_function(*inputs) + self._extra_config[constants.OFFSET]


# PyTorch containers.
class PyTorchSklearnContainer(SklearnContainer):
    """
    Base container for PyTorch models.
    """

    def save(self, location):
        assert self.model is not None, "Saving a None model is undefined."

        if constants.TEST_INPUT in self._extra_config:
            self._extra_config[constants.TEST_INPUT] = None

        assert not os.path.exists(location), "Directory {} already exists.".format(location)
        os.makedirs(location)

        if "torch.jit" in str(type(self.model)):
            # This is a torchscript model.
            # Save the model type.
            with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "w") as file:
                file.write("torch.jit")

            # Save the actual model.
            self.model.save(os.path.join(location, constants.SAVE_LOAD_TORCH_JIT_PATH))

            model = self.model
            self._model = None

            # Save the container.
            with open(os.path.join(location, "container.pkl"), "wb") as file:
                dill.dump(self, file)

            self._model = model
        elif "PyTorchBackendModel" in str(type(self.model)):
            # This is a pytorch model.
            # Save the model type.
            with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "w") as file:
                file.write("torch")

            # Save the actual model plus the container
            with open(os.path.join(location, constants.SAVE_LOAD_TORCH_JIT_PATH), "wb") as file:
                dill.dump(self, file)
        else:
            raise RuntimeError("Model type {} not recognized.".format(type(self.model)))

        # Zip the dir.
        shutil.make_archive(location, "zip", location)

        # Remove the directory.
        shutil.rmtree(location)

    @staticmethod
    def load(location, do_unzip_and_model_type_check=True):
        """
        Method used to load a container from the file system.

        Args:
            location: The location on the file system where to load the model.
            do_unzip_and_model_type_check: Whether to unzip the model and check the type.

        Returns:
            The loaded model.
        """
        container = None

        # Unzip the dir.
        if do_unzip_and_model_type_check:
            zip_location = location
            if not location.endswith("zip"):
                zip_location = location + ".zip"
            else:
                location = zip_location[-4]
            shutil.unpack_archive(zip_location, location, format="zip")

            assert os.path.exists(location), "Model location {} does not exist.".format(location)

        # Load the model type.
        with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "r") as file:
            model_type = file.readline()

        if model_type == "torch.jit":
            # This is a torch.jit model
            model = torch.jit.load(os.path.join(location, constants.SAVE_LOAD_TORCH_JIT_PATH))
            with open(os.path.join(location, "container.pkl"), "rb") as file:
                container = dill.load(file)
            container._model = model
        elif model_type == "torch":
            # This is a pytorch  model
            with open(os.path.join(location, constants.SAVE_LOAD_TORCH_JIT_PATH), "rb") as file:
                container = dill.load(file)
        else:
            raise RuntimeError("Model type {} not recognized".format(model_type))

        # Need to set the number of threads to use as set in the original container.
        if container._n_threads is not None:
            if torch.get_num_interop_threads() != 1:
                torch.set_num_interop_threads(1)
            torch.set_num_threads(container._n_threads)

        return container

    def to(self, device):
        self.model.to(device)
        return self


class PyTorchSklearnContainerTransformer(SklearnContainerTransformer, PyTorchSklearnContainer):
    """
    Container for PyTorch models mirroring Sklearn transformers API.
    """

    def _transform(self, *inputs):
        return self.model.forward(*inputs).cpu().numpy()


class PyTorchSklearnContainerRegression(SklearnContainerRegression, PyTorchSklearnContainer):
    """
    Container for PyTorch models mirroring Sklearn regressor API.
    """

    def _predict(self, *inputs):
        if self._is_regression:
            return self.model.forward(*inputs).cpu().numpy().ravel()
        elif self._is_anomaly_detection:
            return self.model.forward(*inputs)[0].cpu().numpy().ravel()
        else:
            return self.model.forward(*inputs)[0].cpu().numpy().ravel()


class PyTorchSklearnContainerClassification(SklearnContainerClassification, PyTorchSklearnContainerRegression):
    """
    Container for PyTorch models mirroring Sklearn classifiers API.
    """

    def _predict_proba(self, *input):
        return self.model.forward(*input)[1].cpu().numpy()


class PyTorchSklearnContainerAnomalyDetection(PyTorchSklearnContainerRegression, SklearnContainerAnomalyDetection):
    """
    Container for PyTorch models mirroning the Sklearn anomaly detection API.
    """

    def _decision_function(self, *inputs):
        return self.model.forward(*inputs)[1].cpu().numpy().ravel()


# TorchScript containers.
def _torchscript_wrapper(device, function, *inputs, extra_config={}):
    """
    This function contains the code to enable predictions over torchscript models.
    It is used to translates inputs in the proper torch format.
    """
    inputs = [*inputs]

    with torch.no_grad():
        if type(inputs) == DataFrame and DataFrame is not None:
            # Split the dataframe into column ndarrays
            inputs = inputs[0]
            input_names = list(inputs.columns)
            splits = [inputs[input_names[idx]] for idx in range(len(input_names))]
            splits = [df.to_numpy().reshape(-1, 1) for df in splits]
            inputs = tuple(splits)

        # Maps data inputs to the expected type and device.
        for i in range(len(inputs)):
            if type(inputs[i]) is list:
                inputs[i] = np.array(inputs[i])
            if type(inputs[i]) is np.ndarray:
                # Convert string arrays into int32.
                if inputs[i].dtype.kind in constants.SUPPORTED_STRING_TYPES:
                    assert constants.MAX_STRING_LENGTH in extra_config

                    inputs[i] = from_strings_to_ints(inputs[i], extra_config[constants.MAX_STRING_LENGTH])
                if inputs[i].dtype == np.float64:
                    # We convert double precision arrays into single precision. Sklearn does the same.
                    inputs[i] = inputs[i].astype("float32")
                inputs[i] = torch.from_numpy(inputs[i])
            elif type(inputs[i]) is not torch.Tensor:
                raise RuntimeError("Inputer tensor {} of not supported type {}".format(i, type(inputs[i])))
            if device.type != "cpu" and device is not None:
                inputs[i] = inputs[i].to(device)
        return function(*inputs)


class TorchScriptSklearnContainerTransformer(PyTorchSklearnContainerTransformer):
    """
    Container for TorchScript models mirroring Sklearn transformers API.
    """

    def transform(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerTransformer, self)._transform
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs)


class TorchScriptSklearnContainerRegression(PyTorchSklearnContainerRegression):
    """
    Container for TorchScript models mirroring Sklearn regressors API.
    """

    def predict(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerRegression, self)._predict
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs)


class TorchScriptSklearnContainerClassification(PyTorchSklearnContainerClassification):
    """
    Container for TorchScript models mirroring Sklearn classifiers API.
    """

    def predict(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerClassification, self)._predict
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs)

    def predict_proba(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerClassification, self)._predict_proba
        f_wrapped = lambda *x: _torchscript_wrapper(device, f, *x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs)


class TorchScriptSklearnContainerAnomalyDetection(PyTorchSklearnContainerAnomalyDetection):
    """
    Container for TorchScript models mirroring Sklearn anomaly detection API.
    """

    def predict(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerAnomalyDetection, self)._predict
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs)

    def decision_function(self, *inputs):
        device = get_device(self.model)
        f = super(TorchScriptSklearnContainerAnomalyDetection, self)._decision_function
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        scores = self._run(f_wrapped, *inputs)

        if constants.IFOREST_THRESHOLD in self._extra_config:
            scores += self._extra_config[constants.IFOREST_THRESHOLD]
        return scores

    def score_samples(self, *inputs):
        device = get_device(self.model)
        f = self.decision_function
        f_wrapped = lambda x: _torchscript_wrapper(device, f, x, extra_config=self._extra_config)  # noqa: E731

        return self._run(f_wrapped, *inputs) + self._extra_config[constants.OFFSET]


# ONNX containers.
class ONNXSklearnContainer(SklearnContainer):
    """
    Base container for ONNX models.
    The container allows to mirror the Sklearn API.
    """

    def __init__(self, model, n_threads=None, batch_size=None, extra_config={}):
        super(ONNXSklearnContainer, self).__init__(model, n_threads, batch_size, extra_config)

        if onnx_runtime_installed():
            import onnxruntime as ort

            sess_options = ort.SessionOptions()
            if self._n_threads is not None:
                sess_options.intra_op_num_threads = self._n_threads
                sess_options.inter_op_num_threads = 1
                sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self._session = ort.InferenceSession(self._model.SerializeToString(), sess_options=sess_options)
            self._output_names = [self._session.get_outputs()[i].name for i in range(len(self._session.get_outputs()))]
            self._input_names = [input.name for input in self._session.get_inputs()]
            self._extra_config = extra_config
        else:
            raise RuntimeError("ONNX Container requires ONNX runtime installed.")

    def save(self, location):
        assert self.model is not None, "Saving a None model is undefined."
        import onnx

        if constants.TEST_INPUT in self._extra_config:
            self._extra_config[constants.TEST_INPUT] = None

        assert not os.path.exists(location), "Directory {} already exists.".format(location)
        os.makedirs(location)

        # Save the model type.
        with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "w") as file:
            file.write("onnx")

        # Save the actual model.
        onnx.save(self.model, os.path.join(location, constants.SAVE_LOAD_ONNX_PATH))

        model = self.model
        session = self._session
        self._model = None
        self._session = None

        # Save the container.
        with open(os.path.join(location, constants.SAVE_LOAD_CONTAINER_PATH), "wb") as file:
            dill.dump(self, file)

        # Zip the dir.
        shutil.make_archive(location, "zip", location)

        # Remove the directory.
        shutil.rmtree(location)

        self._model = model
        self._session = session

    @staticmethod
    def load(location, do_unzip_and_model_type_check=True):
        """
        Method used to load a container from the file system.

        Args:
            location: The location on the file system where to load the model.
            do_unzip_and_model_type_check: Whether to unzip the model and check the type.

        Returns:
            The loaded model.
        """

        assert onnx_runtime_installed
        import onnx
        import onnxruntime as ort

        container = None

        if do_unzip_and_model_type_check:
            # Unzip the dir.
            zip_location = location
            if not location.endswith("zip"):
                zip_location = location + ".zip"
            else:
                location = zip_location[-4]
            shutil.unpack_archive(zip_location, location, format="zip")

            assert os.path.exists(location), "Model location {} does not exist.".format(location)

            # Load the model type.
            with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "r") as file:
                model_type = file.readline()
                assert model_type == "onnx", "Expected ONNX model type, got {}".format(model_type)

        # Load the actual model.
        model = onnx.load(os.path.join(location, constants.SAVE_LOAD_ONNX_PATH))

        # Load the container.
        with open(os.path.join(location, constants.SAVE_LOAD_CONTAINER_PATH), "rb") as file:
            container = dill.load(file)
        assert container is not None, "Failed to load the model container."

        # Setup the container.
        container._model = model
        sess_options = ort.SessionOptions()
        if container._n_threads is not None:
            # Need to set the number of threads to use as set in the original container.
            sess_options.intra_op_num_threads = container._n_threads
            sess_options.inter_op_num_threads = 1
            sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        container._session = ort.InferenceSession(container._model.SerializeToString(), sess_options=sess_options)

        return container

    def _get_named_inputs(self, inputs):
        """
        Retrieve the inputs names from the session object.
        """
        if len(inputs) < len(self._input_names):
            inputs = inputs[0]

        assert len(inputs) == len(self._input_names)

        named_inputs = {}

        for i in range(len(inputs)):
            input_ = np.array(inputs[i])
            if input_.dtype.kind in constants.SUPPORTED_STRING_TYPES:
                assert constants.MAX_STRING_LENGTH in self._extra_config

                input_ = from_strings_to_ints(input_, self._extra_config[constants.MAX_STRING_LENGTH])
            named_inputs[self._input_names[i]] = input_

        return named_inputs


class ONNXSklearnContainerTransformer(ONNXSklearnContainer, SklearnContainerTransformer):
    """
    Container for ONNX models mirroring Sklearn transformers API.
    """

    def _transform(self, *inputs):
        assert len(self._output_names) == 1
        named_inputs = self._get_named_inputs(inputs)
        return np.array(self._session.run(self._output_names, named_inputs))[0]


class ONNXSklearnContainerRegression(ONNXSklearnContainer, SklearnContainerRegression):
    """
    Container for ONNX models mirroring Sklearn regressors API.
    """

    def _predict(self, *inputs):
        named_inputs = self._get_named_inputs(inputs)

        if self._is_regression:
            assert len(self._output_names) == 1
            return np.array(self._session.run(self._output_names, named_inputs))[0].ravel()
        elif self._is_anomaly_detection:
            assert len(self._output_names) == 2
            return np.array(self._session.run([self._output_names[0]], named_inputs))[0].ravel()
        else:
            assert len(self._output_names) == 2
            return np.array(self._session.run([self._output_names[0]], named_inputs))[0]


class ONNXSklearnContainerClassification(ONNXSklearnContainerRegression, SklearnContainerClassification):
    """
    Container for ONNX models mirroring Sklearn classifiers API.
    """

    def _predict_proba(self, *inputs):
        assert len(self._output_names) == 2

        named_inputs = self._get_named_inputs(inputs)

        return self._session.run([self._output_names[1]], named_inputs)[0]


class ONNXSklearnContainerAnomalyDetection(ONNXSklearnContainerRegression, SklearnContainerAnomalyDetection):
    """
    Container for ONNX models mirroring Sklearn anomaly detection API.
    """

    def _decision_function(self, *inputs):
        assert len(self._output_names) == 2

        named_inputs = self._get_named_inputs(inputs)

        return np.array(self._session.run([self._output_names[1]], named_inputs)[0]).flatten()


# TVM containers.
class TVMSklearnContainer(SklearnContainer):
    """
    Base container for TVM models.
    The container allows to mirror the Sklearn API.
    The test input size must be the same as the batch size this container is created.
    """

    def __init__(self, model, n_threads=None, batch_size=None, extra_config={}):
        super(TVMSklearnContainer, self).__init__(model, n_threads, batch_size, extra_config=extra_config)

        assert tvm_installed()
        import tvm

        self._ctx = self._extra_config[constants.TVM_CONTEXT]
        self._input_names = self._extra_config[constants.TVM_INPUT_NAMES]
        self._to_tvm_array = lambda x: tvm.nd.array(x, self._ctx)

        os.environ["TVM_NUM_THREADS"] = str(self._n_threads)

    def save(self, location):
        assert self.model is not None, "Saving a None model is undefined."
        from tvm import relay

        assert not os.path.exists(location), "Directory {} already exists.".format(location)
        os.makedirs(location)

        # Save the model type.
        with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "w") as file:
            file.write("tvm")

        # Save the actual model.
        path_lib = os.path.join(location, constants.SAVE_LOAD_TVM_LIB_PATH)
        self._extra_config[constants.TVM_LIB].export_library(path_lib)
        with open(os.path.join(location, constants.SAVE_LOAD_TVM_GRAPH_PATH), "w") as fo:
            fo.write(self._extra_config[constants.TVM_GRAPH])
        with open(os.path.join(location, constants.SAVE_LOAD_TVM_PARAMS_PATH), "wb") as fo:
            fo.write(relay.save_param_dict(self._extra_config[constants.TVM_PARAMS]))

        # Remove all information that cannot be pickled
        if constants.TEST_INPUT in self._extra_config:
            self._extra_config[constants.TEST_INPUT] = None
        lib = self._extra_config[constants.TVM_LIB]
        graph = self._extra_config[constants.TVM_GRAPH]
        params = self._extra_config[constants.TVM_PARAMS]
        ctx = self._extra_config[constants.TVM_CONTEXT]
        model = self._model
        self._extra_config[constants.TVM_LIB] = None
        self._extra_config[constants.TVM_GRAPH] = None
        self._extra_config[constants.TVM_PARAMS] = None
        self._extra_config[constants.TVM_CONTEXT] = None
        self._ctx = "cpu" if self._ctx.device_type == 1 else "cuda"
        self._model = None

        # Save the container.
        with open(os.path.join(location, constants.SAVE_LOAD_CONTAINER_PATH), "wb") as file:
            dill.dump(self, file)

        # Zip the dir.
        shutil.make_archive(location, "zip", location)

        # Remove the directory.
        shutil.rmtree(location)

        # Restore the information
        self._extra_config[constants.TVM_LIB] = lib
        self._extra_config[constants.TVM_GRAPH] = graph
        self._extra_config[constants.TVM_PARAMS] = params
        self._extra_config[constants.TVM_CONTEXT] = ctx
        self._ctx = ctx
        self._model = model

    @staticmethod
    def load(location, do_unzip_and_model_type_check=True):
        """
        Method used to load a container from the file system.

        Args:
            location: The location on the file system where to load the model.
            do_unzip_and_model_type_check: Whether to unzip the model and check the type.

        Returns:
            The loaded model.
        """
        assert tvm_installed()
        import tvm
        from tvm.contrib import graph_runtime
        from tvm import relay

        container = None

        if do_unzip_and_model_type_check:
            # Unzip the dir.
            zip_location = location
            if not location.endswith("zip"):
                zip_location = location + ".zip"
            else:
                location = zip_location[-4]
            shutil.unpack_archive(zip_location, location, format="zip")

            assert os.path.exists(location), "Model location {} does not exist.".format(location)

            # Load the model type.
            with open(os.path.join(location, constants.SAVE_LOAD_MODEL_TYPE_PATH), "r") as file:
                model_type = file.readline()
                assert model_type == "tvm", "Expected TVM model type, got {}".format(model_type)

        # Load the actual model.
        path_lib = os.path.join(location, constants.SAVE_LOAD_TVM_LIB_PATH)
        graph = open(os.path.join(location, constants.SAVE_LOAD_TVM_GRAPH_PATH)).read()
        lib = tvm.runtime.module.load_module(path_lib)
        params = relay.load_param_dict(open(os.path.join(location, constants.SAVE_LOAD_TVM_PARAMS_PATH), "rb").read())

        # Load the container.
        with open(os.path.join(location, constants.SAVE_LOAD_CONTAINER_PATH), "rb") as file:
            container = dill.load(file)
        assert container is not None, "Failed to load the model container."

        # Setup the container.
        ctx = tvm.cpu() if container._ctx == "cpu" else tvm.gpu
        container._model = graph_runtime.create(graph, lib, ctx)
        container._model.set_input(**params)

        container._extra_config[constants.TVM_GRAPH] = graph
        container._extra_config[constants.TVM_LIB] = lib
        container._extra_config[constants.TVM_PARAMS] = params
        container._extra_config[constants.TVM_CONTEXT] = ctx
        container._ctx = ctx

        # Need to set the number of threads to use as set in the original container.
        os.environ["TVM_NUM_THREADS"] = str(container._n_threads)

        return container

    def _to_tvm_tensor(self, *inputs):
        tvm_tensors = {}
        msg = "The number of input rows {} is different from the batch size {} the TVM model is compiled for."
        for i, inp in enumerate(inputs):
            assert inp.shape[0] == self._batch_size, msg.format(inp.shape[0], self._batch_size)
            tvm_tensors[self._input_names[i]] = self._to_tvm_array(inp)
        return tvm_tensors

    def _predict_common(self, output_index, *inputs):
        self.model.run(**self._to_tvm_tensor(*inputs))
        return self.model.get_output(output_index).asnumpy()


class TVMSklearnContainerTransformer(TVMSklearnContainer, SklearnContainerTransformer):
    """
    Container for TVM models mirroring Sklearn transformers API.
    """

    def _transform(self, *inputs):
        return self._predict_common(0, *inputs)


class TVMSklearnContainerRegression(TVMSklearnContainer, SklearnContainerRegression):
    """
    Container for TVM models mirroring Sklearn regressors API.
    """

    def _predict(self, *inputs):
        out = self._predict_common(0, *inputs)
        return out.ravel()


class TVMSklearnContainerClassification(TVMSklearnContainerRegression, SklearnContainerClassification):
    """
    Container for TVM models mirroring Sklearn classifiers API.
    """

    def _predict_proba(self, *inputs):
        return self._predict_common(1, *inputs)


class TVMSklearnContainerAnomalyDetection(TVMSklearnContainerRegression, SklearnContainerAnomalyDetection):
    """
    Container for TVM models mirroring Sklearn anomaly detection API.
    """

    def _decision_function(self, *inputs):
        out = self._predict_common(1, *inputs)
        return out.ravel()
