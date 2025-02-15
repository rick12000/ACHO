import logging
from typing import Dict, Optional, List, Tuple, Literal, Union

import random
import numpy as np
from sklearn import metrics
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RationalQuadratic, RBF
from sklearn.kernel_ridge import KernelRidge
from sklearn.metrics import mean_pinball_loss
from sklearn.model_selection import KFold
from sklearn.neighbors import KNeighborsRegressor
from sklearn.neural_network import MLPRegressor
from confopt.preprocessing import train_val_split
from confopt.config import (
    GBM_NAME,
    QRF_NAME,
    QGBM_NAME,
    QKNN_NAME,
    DNN_NAME,
    GP_NAME,
    KNN_NAME,
    KR_NAME,
    RF_NAME,
    QL_NAME,
    QUANTILE_ESTIMATOR_ARCHITECTURES,
)
from confopt.tracking import RuntimeTracker
from confopt.quantile_wrappers import QuantileGBM  # , QuantileKNN, QuantileLasso
from confopt.utils import get_tuning_configurations, get_perceptron_layers

logger = logging.getLogger(__name__)

SEARCH_MODEL_TUNING_SPACE: Dict[str, Dict] = {
    DNN_NAME: {
        "solver": ["adam", "sgd"],
        "learning_rate_init": [0.0001, 0.001, 0.01, 0.1],
        "alpha": [0.0001, 0.001, 0.01, 0.1, 1, 3, 10],
        "hidden_layer_sizes": get_perceptron_layers(
            n_layers_grid=[2, 3, 4], layer_size_grid=[16, 32, 64, 128]
        ),
    },
    RF_NAME: {
        "n_estimators": [25, 50, 100, 150, 200],
        "max_features": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1],
        "min_samples_split": [2, 3, 5],
        "min_samples_leaf": [1, 2, 3],
    },
    KNN_NAME: {"n_neighbors": [1, 2, 3]},
    GBM_NAME: {
        "learning_rate": [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.8],
        "n_estimators": [25, 50, 100, 200],
        "min_samples_split": [2, 3, 5],
        "min_samples_leaf": [1, 3, 5],
        "max_depth": [2, 3, 5, 10],
    },
    GP_NAME: {"kernel": [RBF(), RationalQuadratic()]},
    KR_NAME: {"alpha": [0.001, 0.1, 1, 10], "kernel": ["linear", "rbf", "polynomial"]},
    QRF_NAME: {"n_estimators": [25, 50, 100, 150, 200]},
    QKNN_NAME: {"n_neighbors": [5]},
    QL_NAME: {
        "alpha": [0.01, 0.1, 1.0],
        "max_iter": [500, 1000],
    },
    QGBM_NAME: {
        "learning_rate": [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.8],
        "n_estimators": [25, 50, 100, 200],
        "min_samples_split": [2, 3, 5],
        "min_samples_leaf": [1, 3, 5],
        "max_depth": [2, 3, 5, 10],
    },
}

SEARCH_MODEL_DEFAULT_CONFIGURATIONS: Dict[str, Dict] = {
    DNN_NAME: {
        "solver": "adam",
        "learning_rate_init": 0.001,
        "alpha": 0.1,
        "hidden_layer_sizes": (32, 16),
    },
    RF_NAME: {
        "n_estimators": 50,
        "max_features": 0.8,
        "min_samples_split": 2,
        "min_samples_leaf": 2,
    },
    KNN_NAME: {"n_neighbors": 2},
    GBM_NAME: {
        "learning_rate": 0.1,
        "n_estimators": 50,
        "min_samples_split": 2,
        "min_samples_leaf": 2,
        "max_depth": 3,
    },
    GP_NAME: {"kernel": RBF()},
    KR_NAME: {"alpha": 0.1, "kernel": "rbf"},
    QRF_NAME: {"n_estimators": 50},
    QKNN_NAME: {"n_neighbors": 5},
    QL_NAME: {
        "alpha": 0.1,
        "max_iter": 1000,
    },
    QGBM_NAME: {
        "learning_rate": 0.1,
        "n_estimators": 50,
        "min_samples_split": 2,
        "min_samples_leaf": 2,
        "max_depth": 3,
    },
}


class BaseACI:
    def __init__(self, alpha=0.1, gamma=0.01):
        """
        Base class for Adaptive Conformal Inference (ACI).

        Parameters:
        - alpha: Target coverage level (1 - alpha is the desired coverage).
        - gamma: Step-size parameter for updating alpha_t.
        """
        self.alpha = alpha
        self.gamma = gamma
        self.alpha_t = alpha  # Initial confidence level

    def update(self, breach_indicator):
        """
        Update the confidence level alpha_t based on the breach indicator.

        Parameters:
        - breach_indicator: 1 if the previous prediction breached its interval, 0 otherwise.

        Returns:
        - alpha_t: Updated confidence level.
        """
        raise NotImplementedError("Subclasses must implement the `update` method.")


class ACI(BaseACI):
    def __init__(self, alpha=0.1, gamma=0.01):
        """
        Standard Adaptive Conformal Inference (ACI).

        Parameters:
        - alpha: Target coverage level (1 - alpha is the desired coverage).
        - gamma: Step-size parameter for updating alpha_t.
        """
        super().__init__(alpha, gamma)

    def update(self, breach_indicator):
        """
        Update the confidence level alpha_t using the standard ACI update rule.

        Parameters:
        - breach_indicator: 1 if the previous prediction breached its interval, 0 otherwise.

        Returns:
        - alpha_t: Updated confidence level.
        """
        # Update alpha_t using the standard ACI rule
        self.alpha_t += self.gamma * (self.alpha - breach_indicator)
        self.alpha_t = max(0.01, min(self.alpha_t, 0.99))
        return self.alpha_t


class DtACI(BaseACI):
    def __init__(self, alpha=0.1, gamma_candidates=None, eta=0.1, sigma=0.01):
        """
        Dynamically-Tuned Adaptive Conformal Intervals (DtACI).

        Parameters:
        - alpha (float): Target coverage level (1 - alpha is the desired coverage). Must be between 0 and 1.
        - gamma_candidates (list of float): List of candidate step sizes for the experts. Defaults to a predefined list.
        - eta (float): Learning rate for expert weights. Controls the magnitude of weight adjustments. Must be positive.
        - sigma (float): Exploration rate for expert weights. Small sigma encourages more reliance on the best experts. Must be in [0, 1].
        """
        if not (0 < alpha < 1):
            raise ValueError("alpha must be between 0 and 1.")
        if gamma_candidates is None:
            gamma_candidates = [0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128]
        if any(g <= 0 for g in gamma_candidates):
            raise ValueError("All gamma candidates must be positive.")
        if eta <= 0:
            raise ValueError("eta (learning rate) must be positive.")
        if not (0 <= sigma <= 1):
            raise ValueError("sigma (exploration rate) must be in [0, 1].")

        super().__init__(alpha, gamma=None)  # gamma is not used in DtACI
        self.gamma_candidates = gamma_candidates
        self.eta = eta
        self.sigma = sigma

        # Initialize experts
        self.num_experts = len(self.gamma_candidates)
        self.alpha_t = (
            np.ones(self.num_experts) * alpha
        )  # Initial quantile estimates for each expert
        self.weights = (
            np.ones(self.num_experts) / self.num_experts
        )  # Uniform initial weights

    def update(self, breach_indicator):
        """
        Update the confidence level alpha_t using the DtACI update rule.

        Parameters:
        - breach_indicator (int): 1 if the previous prediction breached its interval, 0 otherwise.

        Returns:
        - float: Updated confidence level, calculated as a weighted average of the experts' estimates.
        """
        if breach_indicator not in [0, 1]:
            raise ValueError("breach_indicator must be either 0 or 1.")

        # Update each expert's alpha estimate based on the breach indicator
        for i in range(self.num_experts):
            self.alpha_t[i] += self.gamma_candidates[i] * (
                self.alpha - breach_indicator
            )

        # Update expert weights using the exponential weighting scheme
        losses = np.abs(
            self.alpha - breach_indicator
        )  # Pinball loss simplifies to breach indicator here
        self.weights *= np.exp(-self.eta * losses)

        # Normalize weights to prevent underflow or overflow
        self.weights = (1 - self.sigma) * self.weights / np.sum(
            self.weights
        ) + self.sigma / self.num_experts

        # Compute the final alpha_t as a weighted average of experts' alpha estimates
        final_alpha_t = np.dot(self.weights, self.alpha_t)

        # Ensure final_alpha_t stays within valid bounds [0, 1]
        final_alpha_t = np.clip(final_alpha_t, 0, 1)

        return final_alpha_t


def initialize_point_estimator(
    estimator_architecture: str,
    initialization_params: Dict,
    random_state: Optional[int] = None,
):
    """
    Initialize a point estimator from an input dictionary.

    Classes are usually scikit-learn estimators and dictionaries must
    contain all required inputs for the class, in addition to any
    optional inputs to be overridden.

    Parameters
    ----------
    estimator_architecture :
        String name for the type of estimator to initialize.
    initialization_params :
        Dictionary of initialization parameters, where each key and
        value pair corresponds to a variable name and variable value
        to pass to the estimator class to initialize.
    random_state :
        Random generation seed.

    Returns
    -------
    initialized_model :
        An initialized estimator class instance.
    """
    if estimator_architecture == DNN_NAME:
        initialized_model = MLPRegressor(
            **initialization_params, random_state=random_state
        )
    elif estimator_architecture == RF_NAME:
        initialized_model = RandomForestRegressor(
            **initialization_params, random_state=random_state
        )
    elif estimator_architecture == KNN_NAME:
        initialized_model = KNeighborsRegressor(**initialization_params)
    elif estimator_architecture == GBM_NAME:
        initialized_model = GradientBoostingRegressor(
            **initialization_params, random_state=random_state
        )
    elif estimator_architecture == GP_NAME:
        initialized_model = GaussianProcessRegressor(
            **initialization_params, random_state=random_state
        )
    elif estimator_architecture == KR_NAME:
        initialized_model = KernelRidge(**initialization_params)
    else:
        raise ValueError(
            f"{estimator_architecture} is not a valid point estimator architecture."
        )

    return initialized_model


def initialize_quantile_estimator(
    estimator_architecture: str,
    initialization_params: Dict,
    pinball_loss_alpha: List[float],
    random_state: Optional[int] = None,
):
    """
    Initialize a quantile estimator from an input dictionary.

    Classes are usually external dependancies or custom wrappers or
    scikit-learn estimator classes. Passed dictionaries must
    contain all required inputs for the class, in addition to any
    optional inputs to be overridden.

    Parameters
    ----------
    estimator_architecture :
        String name for the type of estimator to initialize.
    initialization_params :
        Dictionary of initialization parameters, where each key and
        value pair corresponds to a variable name and variable value
        to pass to the estimator class to initialize.
    pinball_loss_alpha :
        List of pinball loss alpha levels that will result in the
        estimator predicting the alpha-corresponding quantiles.
        For eg. passing [0.25, 0.75] will initialize a quantile
        estimator that predicts the 25th and 75th percentiles of
        the data.
    random_state :
        Random generation seed.

    Returns
    -------
    initialized_model :
        An initialized estimator class instance.
    """
    if estimator_architecture == QGBM_NAME:
        initialized_model = QuantileGBM(
            **initialization_params,
            quantiles=pinball_loss_alpha,
            random_state=random_state,
        )
    # elif estimator_architecture == QKNN_NAME:
    #     initialized_model = QuantileKNN(
    #         **initialization_params,
    #         quantiles=pinball_loss_alpha,
    #         random_state=random_state,
    #     )
    # elif estimator_architecture == QL_NAME:
    #     initialized_model = QuantileLasso(
    #         **initialization_params,
    #         quantiles=pinball_loss_alpha,
    #         random_state=random_state,
    #     )
    else:
        raise ValueError(
            f"{estimator_architecture} is not a valid estimator architecture."
        )

    return initialized_model


def average_scores_across_folds(
    scored_configurations: List[List[Tuple[str, float]]], scores: List[float]
) -> Tuple[List[List[Tuple[str, float]]], List[float]]:
    # Use a list to store aggregated scores and fold counts
    aggregated_scores = []
    fold_counts = []
    aggregated_configurations = []

    for configuration, score in zip(scored_configurations, scores):
        # Check if the configuration already exists in the aggregated_configurations list
        if configuration in aggregated_configurations:
            index = aggregated_configurations.index(configuration)
            aggregated_scores[index] += score
            fold_counts[index] += 1
        else:
            aggregated_configurations.append(configuration)
            aggregated_scores.append(score)
            fold_counts.append(1)

    # Calculate the average scores
    for i in range(len(aggregated_scores)):
        aggregated_scores[i] /= fold_counts[i]

    return aggregated_configurations, aggregated_scores


def cross_validate_configurations(
    configurations: List[Dict],
    estimator_architecture: str,
    X: np.array,
    y: np.array,
    k_fold_splits: int = 3,
    quantiles: Optional[List[float]] = None,
    random_state: Optional[int] = None,
) -> Tuple[List[Dict], List[float]]:
    """
    Cross validate a specified estimator on a passed X, y dataset.

    Cross validation loops through a list of passed hyperparameter
    configurations for the previously specified estimator and returns
    an average score across folds for each.

    Parameters
    ----------
    configurations :
        List of estimator parameter configurations, where each
        configuration contains all parameter values necessary
        to create an estimator instance.
    estimator_architecture :
        String name for the type of estimator to cross validate.
    X :
        Explanatory variables to train estimator on.
    y :
        Target variable to train estimator on.
    k_fold_splits :
        Number of cross validation data splits.
    quantiles :
        If the estimator to cross validate is a quantile estimator,
        specify the quantiles it should estimate as a list in this
        variable (eg. [0.25, 0.75] will cross validate an estimator
        predicting the 25th and 75th percentiles of the target variable).
    random_state :
        Random generation seed.

    Returns
    -------
    cross_fold_scored_configurations :
        List of cross validated configurations.
    cross_fold_scores :
        List of corresponding cross validation scores (averaged across
        folds).
    """
    scored_configurations, scores = [], []
    kf = KFold(n_splits=k_fold_splits, random_state=random_state, shuffle=True)
    for train_index, test_index in kf.split(X):
        X_train, X_val = X[train_index, :], X[test_index, :]
        Y_train, Y_val = y[train_index], y[test_index]

        for configuration in configurations:
            logger.debug(
                f"Evaluating search model parameter configuration: {configuration}"
            )
            if estimator_architecture in QUANTILE_ESTIMATOR_ARCHITECTURES:
                if quantiles is None:
                    raise ValueError(
                        "'quantiles' cannot be None if passing a quantile regression estimator."
                    )
                else:
                    model = initialize_quantile_estimator(
                        estimator_architecture=estimator_architecture,
                        initialization_params=configuration,
                        pinball_loss_alpha=quantiles,
                        random_state=random_state,
                    )
            else:
                model = initialize_point_estimator(
                    estimator_architecture=estimator_architecture,
                    initialization_params=configuration,
                    random_state=random_state,
                )
            model.fit(X_train, Y_train)
            y_pred = model.predict(X_val)

            try:
                if estimator_architecture in QUANTILE_ESTIMATOR_ARCHITECTURES:
                    if quantiles is None:
                        raise ValueError(
                            "'quantiles' cannot be None if passing a quantile regression estimator."
                        )
                    else:
                        # Then evaluate on pinball loss:
                        lo_y_pred = model.predict(X_val)[:, 0]
                        hi_y_pred = model.predict(X_val)[:, 1]
                        lo_score = mean_pinball_loss(
                            Y_val, lo_y_pred, alpha=quantiles[0]
                        )
                        hi_score = mean_pinball_loss(
                            Y_val, hi_y_pred, alpha=quantiles[1]
                        )
                        score = (lo_score + hi_score) / 2
                else:
                    # Then evaluate on MSE:
                    score = metrics.mean_squared_error(Y_val, y_pred)

                scored_configurations.append(configuration)
                scores.append(score)

            except Exception as e:
                logger.warning(
                    "Scoring failed and result was not appended."
                    f"Caught exception: {e}"
                )
                continue

    cross_fold_scored_configurations, cross_fold_scores = average_scores_across_folds(
        scored_configurations=scored_configurations, scores=scores
    )

    return cross_fold_scored_configurations, cross_fold_scores


# class BayesUCBSampler:
#     def __init__(self, c: float = 1, n: float = 50):
#         self.c = c
#         self.n = n
#         self.t = 1

#     def fetch_quantiles(self):
#         lower_bound_quantile = 1 / (self.t * (np.log(self.n) ** self.c))
#         quantiles = [lower_bound_quantile, 1 - lower_bound_quantile]
#         return quantiles

#     def update_exploration_step(self):
#         self.t = self.t + 1


class QuantileInterval:
    def __init__(self, lower_quantile_level: float, upper_quantile_level: float):
        self.lower_quantile_level = lower_quantile_level
        self.upper_quantile_level = upper_quantile_level

    def to_list(self):
        return [self.lower_quantile_level, self.upper_quantile_level]


class QuantileIntervalSequence:
    def __init__(
        self, quantile_interval_sequence: Optional[list[QuantileInterval]] = None
    ):
        self.quantile_interval_sequence = quantile_interval_sequence

    def append(self, quantile_interval: QuantileInterval):
        self.quantile_interval_sequence.append(quantile_interval)

    def extend(self, quantile_intervals: list[QuantileInterval]):
        self.quantile_interval_sequence.extend(quantile_intervals)

    def to_flattened_list(self):
        flattened_list = []
        for quantile_interval in self.quantile_interval_sequence:
            flattened_list.extend(quantile_interval.to_list())

        flattened_list.sort()

        return flattened_list

    def from_flattened_list(self, flattened_list: list[float]):
        flattened_list.sort()
        quantile_interval_sequence = []
        for i in range(int(len(flattened_list) / 2)):
            quantile_interval_sequence.append(
                QuantileInterval(
                    lower_quantile_level=flattened_list[0 + i],
                    upper_quantile_level=flattened_list[-1 - i],
                )
            )

        return QuantileIntervalSequence(
            quantile_interval_sequence=quantile_interval_sequence
        )


class UCBSampler:
    def __init__(
        self,
        beta_decay: Literal[
            "logarithmic_growth", "logarithmic_decay"
        ] = "logarithmic_decay",
        beta: float = 1,
        c: float = 1,
        interval_width: float = 0.2,
        adapter_framework: Optional[Literal["ACI", "DtACI"]] = None,
    ):
        self.beta_decay = beta_decay
        self.beta = beta
        self.c = c
        self.interval_width = interval_width

        self.alpha = 1 - self.interval_width
        if adapter_framework is not None:
            if adapter_framework == "ACI":
                self.adapter = ACI(alpha=self.alpha)
            elif adapter_framework == "DtACI":
                self.adapter = DtACI(alpha=self.alpha)
        self.quantiles = [self.alpha / 2, 1 - (self.alpha / 2)]
        self.t = 1

    def fetch_alpha(self):
        return self.alpha

    def fetch_quantiles(self):
        return QuantileInterval(self.quantiles[0], self.quantiles[1])

    def update_exploration_step(self):
        if self.beta_decay == "logarithmic_decay":
            self.beta = self.c * np.log(self.t) / self.t
        elif self.beta_decay == "logarithmic_growth":
            self.beta = 2 * np.log(self.t + 1)
        self.t = self.t + 1

    def update_interval_width(self, breach: int):
        self.alpha = self.adapter.update(breach_indicator=breach)
        self.quantiles = [self.alpha / 2, 1 - (self.alpha / 2)]


class ThompsonSampler:
    def __init__(
        self,
        n_quantiles: int = 4,
        adapter_framework: Optional[Literal["ACI", "DtACI"]] = None,
    ):
        if n_quantiles % 2 != 0:
            raise ValueError("Number of Thompson quantiles must be even.")
        self.n_quantiles = n_quantiles

        self.quantiles = [
            round(i * 1 / (self.n_quantiles + 1), 2)
            for i in range(1, self.n_quantiles + 1)
        ]

        self.alphas = []
        for i in range(int(len(self.quantiles) / 2)):
            interval = self.quantiles[-1 - i] - self.quantiles[0 + i]
            alpha = 1 - interval
            self.alphas.append(alpha)

        if adapter_framework is not None:
            if adapter_framework == "ACI":
                self.adapters: list[ACI] = []
                for alpha in self.alphas:
                    self.adapters.append(ACI(alpha=alpha))
            elif adapter_framework == "DtACI":
                self.adapters: list[DtACI] = []
                for alpha in self.alphas:
                    self.adapters.append(DtACI(alpha=alpha))

    def fetch_alphas(self):
        return self.alphas

    def fetch_quantiles(self) -> QuantileIntervalSequence:
        quantile_intervals_sequence = QuantileIntervalSequence().from_flattened_list(
            flattened_list=self.quantiles
        )
        return quantile_intervals_sequence

    def update_interval_width(self, breaches: list[int]):
        alphas = []
        quantiles = []
        for adapter, breach_indicator in zip(self.adapters, breaches):
            alpha = adapter.update(breach_indicator=breach_indicator)
            alphas.append(alpha)
            quantiles.extend([alpha / 2, 1 - (alpha / 2)])
        self.alphas = alphas
        quantiles.sort()
        self.quantiles = quantiles


class LocallyWeightedConformalSearcher:
    """
    Locally weighted conformal regression.

    Fits sequential estimators on X and y data to form point and
    variability predictions for y.

    The class contains tuning, fitting and prediction methods.
    """

    def __init__(
        self,
        point_estimator_architecture: str,
        variance_estimator_architecture: str,
        sampler: Union[UCBSampler, ThompsonSampler],
        demeaning_estimator_architecture: Optional[str] = None,
    ):
        self.point_estimator_architecture = point_estimator_architecture
        self.demeaning_estimator_architecture = demeaning_estimator_architecture
        self.variance_estimator_architecture = variance_estimator_architecture
        self.sampler = sampler

        self.training_time = None

    def _tune_component_estimator(
        self,
        X: np.array,
        y: np.array,
        estimator_architecture: str,
        n_searches: int,
        k_fold_splits: int = 3,
        random_state: Optional[int] = None,
    ) -> Dict:
        """
        Tune specified estimator's hyperparameters.

        Hyperparameters are selected randomly as part of the
        tuning process and a final optimal hyperparameter
        configuration is returned.

        Parameters
        ----------
        X :
            Explanatory variables.
        y :
            Target variable.
        estimator_architecture :
            String name for the type of estimator to tune.
        n_searches :
            Number of tuning searches to perform (eg. 5 means
            the model will randomly select 5 hyperparameter
            configurations for the estimator to evaluate).
        k_fold_splits :
            Number of cross validation data splits.
        random_state :
            Random generation seed.

        Returns
        -------
        best_configuration :
            Best performing hyperparameter configuration
            in tuning.
        """
        tuning_configurations = get_tuning_configurations(
            parameter_grid=SEARCH_MODEL_TUNING_SPACE[estimator_architecture],
            n_configurations=n_searches,
            random_state=random_state,
        )
        tuning_configurations.append(
            SEARCH_MODEL_DEFAULT_CONFIGURATIONS[estimator_architecture]
        )

        scored_configurations, scores = cross_validate_configurations(
            configurations=tuning_configurations,
            estimator_architecture=estimator_architecture,
            X=X,
            y=y,
            k_fold_splits=k_fold_splits,
            quantiles=None,
            random_state=random_state,
        )
        best_configuration = scored_configurations[scores.index(min(scores))]

        return best_configuration

    def _fit_component_estimator(
        self,
        X,
        y,
        estimator_architecture,
        tuning_iterations,
        random_state: Optional[int] = None,
    ):
        """
        Fit component estimator with option to tune.

        Component estimators are loosely defined, general use
        point estimators. Their final purpose is dependent on
        what X and y data is passed to the function (eg. if y is
        a target, a residual, etc.).

        Parameters
        ----------
        X :
            Explanatory variables.
        y :
            Target variable.
        estimator_architecture :
            String name for the type of estimator to tune.
        tuning_iterations :
            Number of tuning searches to perform (eg. 5 means
            the model will randomly select 5 hyperparameter
            configurations for the estimator to evaluate).
            To skip tuning during fitting, set this to 0.
        random_state :
            Random generation seed.

        Returns
        -------
        estimator :
            Fitted estimator object.
        """
        if tuning_iterations > 1:
            initialization_params = self._tune_component_estimator(
                X=X,
                y=y,
                estimator_architecture=estimator_architecture,
                n_searches=tuning_iterations,
                random_state=random_state,
            )
        else:
            initialization_params = SEARCH_MODEL_DEFAULT_CONFIGURATIONS[
                estimator_architecture
            ].copy()
        estimator = initialize_point_estimator(
            estimator_architecture=estimator_architecture,
            initialization_params=initialization_params,
            random_state=random_state,
        )
        self.training_time_tracker.resume_runtime()
        estimator.fit(X, y)
        self.training_time_tracker.pause_runtime()

        return estimator

    def fit(
        self,
        X_train: np.array,
        y_train: np.array,
        X_val: np.array,
        y_val: np.array,
        tuning_iterations: Optional[int] = 0,
        random_state: Optional[int] = None,
    ):
        """
        Fit conformal regression model on specified data.

        Fitting process involves the following sequential steps:
            1.  Fitting an estimator on a first portion of the
                data, training on X to predict y.
            2.  Obtaining residuals between the estimator and
                observed y's on a second portion of the data.
            3.  Fitting a conditional mean estimator on the
                residual data.
            4.  Using the mean estimator to de-mean the residual
                data.
            5.  Fitting an estimator to predict absolute, de-meaned
                residuals (residual spread around the local mean).
            6.  Using a third portion of the data as a conformal
                hold out set to calibrate intervals for the estimator.

        Parameters
        ----------
        X_pe :
            Explanatory variables used to train the point estimator.
        y_pe :
            Target variable used to train the point estimator.
        X_ve :
            Explanatory variables used to train the residual spread
            (variability) estimator.
        y_ve :
            Target variable used to train the residual spread
            (variability) estimator.
        X_val :
            Explanatory variables used to calibrate the point estimator.
        y_val :
            Target variable used to calibrate the point estimator.
        tuning_iterations :
            Number of tuning searches to perform (eg. 5 means
            the model will randomly select 5 hyperparameter
            configurations for the estimator to evaluate).
            To skip tuning during fitting, set this to 0.
        random_state :
            Random generation seed.
        """
        (X_pe, y_pe, X_ve, y_ve,) = train_val_split(
            X_train,
            y_train,
            train_split=0.75,
            normalize=False,
            random_state=random_state,
        )
        logger.debug(
            f"Obtained sub training set of size {X_pe.shape} "
            f"and sub validation set of size {X_ve.shape}"
        )

        self.training_time_tracker = RuntimeTracker()
        self.training_time_tracker.pause_runtime()

        self.pe_estimator = self._fit_component_estimator(
            X=X_pe,
            y=y_pe,
            estimator_architecture=self.point_estimator_architecture,
            tuning_iterations=tuning_iterations,
            random_state=random_state,
        )

        pe_residuals = y_ve - self.pe_estimator.predict(X_ve)

        if self.demeaning_estimator_architecture is not None:
            de_estimator = self._fit_component_estimator(
                X=X_ve,
                y=pe_residuals,
                estimator_architecture=self.demeaning_estimator_architecture,
                tuning_iterations=tuning_iterations,
                random_state=random_state,
            )
            abs_pe_residuals = abs(pe_residuals - de_estimator.predict(X_ve))
        else:
            abs_pe_residuals = abs(pe_residuals)

        self.ve_estimator = self._fit_component_estimator(
            X=X_ve,
            y=abs_pe_residuals,
            estimator_architecture=self.variance_estimator_architecture,
            tuning_iterations=tuning_iterations,
            random_state=random_state,
        )
        var_pred = self.ve_estimator.predict(X_val)
        var_pred = np.array([1 if x <= 0 else x for x in var_pred])

        self.nonconformity_scores = (
            abs(np.array(y_val) - self.pe_estimator.predict(X_val)) / var_pred
        )
        self.training_time = self.training_time_tracker.return_runtime()

    def predict(self, X: np.array):
        """
        Predict conformal interval bounds for specified X examples.

        Must be called after a relevant conformal estimator has
        been trained.

        Parameters
        ----------
        X :
            Explanatory variables to return targets for.
        confidence_level :
            Confidence level used to generate intervals.

        Returns
        -------
        lower_interval_bound :
            Lower bound(s) of conformal interval for specified
            X example(s).
        upper_interval_bound :
            Upper bound(s) of conformal interval for specified
            X example(s).
        """
        y_pred = np.array(self.pe_estimator.predict(X))

        var_pred = self.ve_estimator.predict(X)
        var_pred = np.array([max(x, 0) for x in var_pred])

        if isinstance(self.sampler, UCBSampler):
            score_quantile = np.quantile(
                self.nonconformity_scores, self.sampler.fetch_alpha()
            )
            scaled_score = score_quantile * var_pred
            self.adjusted_predictions = np.empty((0, 0))
            self.adjusted_predictions = np.hstack(
                (self.adjusted_predictions, y_pred - self.sampler.beta * scaled_score)
            )
            self.adjusted_predictions = np.hstack(
                (self.adjusted_predictions, y_pred + self.sampler.beta * scaled_score)
            )
            lower_bound = self.adjusted_predictions[:, 0]

            self.sampler.update_exploration_step()

        elif isinstance(self.sampler, ThompsonSampler):
            self.adjusted_predictions = np.empty((0, 0))
            for alpha in self.sampler.fetch_alphas():
                score_quantile = np.quantile(self.nonconformity_scores, alpha)
                scaled_score = score_quantile * var_pred
                self.adjusted_predictions = np.hstack(
                    (self.adjusted_predictions, y_pred - scaled_score)
                )
                self.adjusted_predictions = np.hstack(
                    (self.adjusted_predictions, y_pred + scaled_score)
                )

            lower_bound = []
            for i in range(self.adjusted_predictions.shape[0]):
                ts_idx = random.choice(range(self.sampler.n_quantiles))
                lower_bound.append(self.adjusted_predictions[i, ts_idx])
            lower_bound = np.array(lower_bound)

        return lower_bound

    def update_interval_width(self, sampled_idx: int, sampled_performance: float):
        if isinstance(self.sampler, UCBSampler):
            sample_quantiles = [
                self.adjusted_predictions[sampled_idx, 0],
                self.adjusted_predictions[sampled_idx, 1],
            ]
            if sample_quantiles[0] <= sampled_performance <= sample_quantiles[1]:
                breach = 0
            else:
                breach = 1
            self.sampler.update_interval_width(breach=breach)

        elif isinstance(self.sampler, ThompsonSampler):
            sample_quantiles = list(self.adjusted_predictions[sampled_idx, :])
            # TODO
            # quantile_sequence = QuantileIntervalSequence.from_flattened_list(sample_quantiles)
            # breaches = []
            # for quantile_interval in quantile_sequence:
            #     if quantile_interval.lower_quantile_level <= sampled_performance <= quantile_interval.upper_quantile_level:
            #         breach = 0
            #     else:
            #         breach = 1
            #     breaches.append(breach)
            # self.sampler.update_interval_width(breaches=breaches)


class QuantileConformalRegression:
    """
    Quantile conformal regression.

    Fits quantile estimators on X and y data and applies non-conformity
    adjustments to validate quantile estimates.

    The class contains tuning, fitting and prediction methods.
    """

    def __init__(
        self,
        quantile_estimator_architecture: str,
        sampler: Union[UCBSampler, ThompsonSampler],
        n_pre_conformal_trials: int = 20,
    ):
        self.quantile_estimator_architecture = quantile_estimator_architecture
        self.sampler = sampler
        self.n_pre_conformal_trials = n_pre_conformal_trials

        self.training_time = None

    def _tune(
        self,
        X: np.array,
        y: np.array,
        estimator_architecture: str,
        n_searches: int,
        quantiles: List[float],
        k_fold_splits: int = 3,
        random_state: Optional[int] = None,
    ) -> Dict:
        tuning_configurations = get_tuning_configurations(
            parameter_grid=SEARCH_MODEL_TUNING_SPACE[estimator_architecture],
            n_configurations=n_searches,
            random_state=random_state,
        )
        tuning_configurations.append(
            SEARCH_MODEL_DEFAULT_CONFIGURATIONS[estimator_architecture]
        )

        scored_configurations, scores = cross_validate_configurations(
            configurations=tuning_configurations,
            estimator_architecture=estimator_architecture,
            X=X,
            y=y,
            k_fold_splits=k_fold_splits,
            quantiles=quantiles,
            random_state=random_state,
        )
        best_configuration = scored_configurations[scores.index(min(scores))]

        return best_configuration

    def fit(
        self,
        X_train: np.array,
        y_train: np.array,
        X_val: np.array,
        y_val: np.array,
        tuning_iterations: Optional[int] = 0,
        random_state: Optional[int] = None,
    ):
        """
        Fit quantile estimator with option to tune.

        Quantile estimators are fitted based on a specified confidence
        level and return two quantile estimates for the symmetrical
        lower and upper bounds around that level.

        Parameters
        ----------
        X_train :
            Explanatory variables used to train the quantile estimator.
        y_train :
            Target variable used to train the quantile estimator.
        X_val :
            Explanatory variables used to calibrate conformal intervals.
        y_val :
            Target variable used to calibrate conformal intervals.
        confidence_level :
            Confidence level determining quantiles to be predicted
            by the quantile estimator. Quantiles are obtained symmetrically
            around the confidence level (eg. 0.5 confidence level would
            result in a quantile estimator for the 25th and 75th percentiles
            of the target variable).
        tuning_iterations :
            Number of tuning searches to perform (eg. 5 means
            the model will randomly select 5 hyperparameter
            configurations for the quantile estimator to evaluate).
            To skip tuning during fitting, set this to 0.
        random_state :
            Random generation seed.

        Returns
        -------
        estimator :
            Fitted estimator object.
        """
        if isinstance(self.sampler, UCBSampler):
            quantile_interval = self.sampler.fetch_quantiles()
            self.quantiles = quantile_interval.to_list()
            self.quantiles.insert(1, 0.5)
        elif isinstance(self.sampler, ThompsonSampler):
            quantile_intervals = self.sampler.fetch_quantiles()
            self.quantiles = quantile_intervals.to_flattened_list()

        if tuning_iterations > 1:
            initialization_params = self._tune(
                X=X_train,
                y=y_train,
                estimator_architecture=self.quantile_estimator_architecture,
                n_searches=tuning_iterations,
                quantiles=self.quantiles,
                random_state=random_state,
            )
        else:
            initialization_params = SEARCH_MODEL_DEFAULT_CONFIGURATIONS[
                self.quantile_estimator_architecture
            ].copy()

        self.quantile_estimator = initialize_quantile_estimator(
            estimator_architecture=self.quantile_estimator_architecture,
            initialization_params=initialization_params,
            pinball_loss_alpha=self.quantiles,
            random_state=random_state,
        )
        training_time_tracker = RuntimeTracker()
        if len(X_train) + len(X_val) > self.n_pre_conformal_trials:
            self.quantile_estimator.fit(X_train, y_train)
            self.training_time = training_time_tracker.return_runtime()
            if isinstance(self.sampler, UCBSampler):
                self.indexed_nonconformity_scores = {}
                lower_conformal_deviations = list(
                    self.quantile_estimator.predict(X_val)[:, 0] - y_val
                )
                upper_conformal_deviations = list(
                    y_val - self.quantile_estimator.predict(X_val)[:, -1]
                )
                nonconformity_scores = []
                for lower_deviation, upper_deviation in zip(
                    lower_conformal_deviations, upper_conformal_deviations
                ):
                    nonconformity_scores.append(max(lower_deviation, upper_deviation))
                self.indexed_nonconformity_scores[0] = np.array(nonconformity_scores)
                self.indexed_nonconformity_scores[-1] = np.array(nonconformity_scores)

            elif isinstance(self.sampler, ThompsonSampler):
                self.indexed_nonconformity_scores = {}
                for i in range(int(len(self.quantiles) / 2)):
                    lower_conformal_deviations = list(
                        self.quantile_estimator.predict(X_val)[:, 0 + i] - y_val
                    )
                    upper_conformal_deviations = list(
                        y_val
                        - self.quantile_estimator.predict(X_val)[
                            :, self.sampler.n_quantiles - 1 - i
                        ]
                    )
                    nonconformity_scores = []
                    for lower_deviation, upper_deviation in zip(
                        lower_conformal_deviations, upper_conformal_deviations
                    ):
                        nonconformity_scores.append(
                            max(lower_deviation, upper_deviation)
                        )
                    self.indexed_nonconformity_scores[0 + i] = np.array(
                        nonconformity_scores
                    )
                    self.indexed_nonconformity_scores[
                        self.sampler.n_quantiles - 1 - i
                    ] = np.array(nonconformity_scores)
            self.conformalize_predictions = True

        else:
            self.quantile_estimator.fit(
                np.vstack((X_train, X_val)), np.concatenate((y_train, y_val))
            )
            self.training_time = training_time_tracker.return_runtime()

            self.conformalize_predictions = False

    def predict(self, X: np.array):
        """
        Predict conformal interval bounds for specified X examples.

        Must be called after a relevant quantile estimator has
        been trained. Intervals will be generated based on a passed
        confidence level, which should ideally be the same confidence
        level specified in training, but may differ (though this is
        less desirable and there should rarely be a valid reason).

        Parameters
        ----------
        X :
            Explanatory variables to return targets for.
        confidence_level :
            Confidence level used to generate intervals.

        Returns
        -------
        lower_interval_bound :
            Lower bound(s) of conformal interval for specified
            X example(s).
        upper_interval_bound :
            Upper bound(s) of conformal interval for specified
            X example(s).
        """
        if isinstance(self.sampler, UCBSampler):
            if self.conformalize_predictions:
                score_quantile = np.quantile(
                    self.indexed_nonconformity_scores[0],
                    self.sampler.fetch_quantiles().lower_quantile_level,
                )
            else:
                score_quantile = 0
            self.lower_interval_bound = (
                np.array(self.quantile_estimator.predict(X)[:, 0]) - score_quantile
            )
            self.upper_interval_bound = (
                np.array(self.quantile_estimator.predict(X)[:, -1]) + score_quantile
            )
            lower_bound = np.array(
                self.quantile_estimator.predict(X)[:, 1]
            ) + self.sampler.beta * (
                np.array(self.quantile_estimator.predict(X)[:, 1])
                - self.lower_interval_bound
            )

            self.sampler.update_exploration_step()
        elif isinstance(self.sampler, ThompsonSampler):
            if self.conformalize_predictions:
                score_quantiles = []
                for i in range(self.sampler.n_quantiles):
                    score = np.quantile(
                        self.indexed_nonconformity_scores[i],
                        self.sampler.fetch_quantiles().to_flattened_list()[i],
                    )
                    if i < self.sampler.n_quantiles / 2:
                        score_quantiles.append(-score)
                    else:
                        score_quantiles.append(score)
            else:
                score_quantiles = [0] * self.sampler.n_quantiles

            predictions = self.quantile_estimator.predict(X)
            self.adjusted_predictions = (
                predictions + np.array(score_quantiles).reshape(-1, 1).T
            )
            lower_bound = []
            for i in range(self.adjusted_predictions.shape[0]):
                ts_idx = random.choice(range(self.sampler.n_quantiles))
                lower_bound.append(self.adjusted_predictions[i, ts_idx])
            lower_bound = np.array(lower_bound)

        return lower_bound

    def update_interval_width(self, sampled_idx: int, sampled_performance: float):
        if isinstance(self.sampler, UCBSampler):
            sample_quantiles = [
                self.lower_interval_bound[sampled_idx],
                self.upper_interval_bound[sampled_idx],
            ]
            if sample_quantiles[0] <= sampled_performance <= sample_quantiles[1]:
                breach = 0
            else:
                breach = 1
            self.sampler.update_interval_width(breach=breach)

        elif isinstance(self.sampler, ThompsonSampler):
            sample_quantiles = list(self.adjusted_predictions[sampled_idx, :])
            quantile_sequence = QuantileIntervalSequence().from_flattened_list(
                flattened_list=sample_quantiles
            )
            breaches = []
            for quantile_interval in quantile_sequence.quantile_interval_sequence:
                if (
                    quantile_interval.lower_quantile_level
                    <= sampled_performance
                    <= quantile_interval.upper_quantile_level
                ):
                    breach = 0
                else:
                    breach = 1
                breaches.append(breach)
            self.sampler.update_interval_width(breaches=breaches)
