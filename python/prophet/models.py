# -*- coding: utf-8 -*-
# Copyright (c) Facebook, Inc. and its affiliates.

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import absolute_import, division, print_function
import dataclasses

import logging
import platform
from abc import ABC, abstractmethod
from collections import OrderedDict
from enum import Enum
from typing import Dict, Tuple, Union

import importlib_resources
import numpy as np

logger = logging.getLogger("prophet.models")

PLATFORM = "win" if platform.platform().startswith("Win") else "unix"


class IStanBackend(ABC):
    def __init__(self):
        self.model = self.load_model()
        self.stan_fit = None
        self.newton_fallback = True

    def set_options(self, **kwargs):
        """
        Specify model options as kwargs.
         * newton_fallback [bool]: whether to fallback to Newton if L-BFGS fails
        """
        for k, v in kwargs.items():
            if k == "newton_fallback":
                self.newton_fallback = v
            else:
                raise ValueError(f"Unknown option {k}")

    @staticmethod
    def simplify_mcmc_results(params: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """Simplifies dimensions of results from MCMC sampling with multiple chains."""
        out = {k: v for k, v in params.items()}
        for par in out:
            s = out[par].shape
            if s[1] == 1:
                out[par] = out[par].reshape((s[0],))
            if par in ["delta", "beta"] and len(s) < 2:
                out[par] = out[par].reshape((-1, 1))
        return out

    @staticmethod
    def sanitize_custom_inits(default_inits, custom_inits):
        """Validate that custom inits have the correct type and shape, otherwise use defaults."""
        sanitized = {}
        for param in ["k", "m", "sigma_obs"]:
            try:
                sanitized[param] = float(custom_inits.get(param))
            except Exception:
                sanitized[param] = default_inits[param]
        for param in ["delta", "beta"]:
            if default_inits[param].shape == custom_inits[param].shape:
                sanitized[param] = custom_inits[param]
            else:
                sanitized[param] = default_inits[param]
        return sanitized

    @staticmethod
    @abstractmethod
    def get_type():
        pass

    @abstractmethod
    def load_model(self):
        pass

    @abstractmethod
    def fit(self, stan_init, stan_data, **kwargs) -> dict:
        pass

    @abstractmethod
    def sampling(self, stan_init, stan_data, samples, **kwargs) -> dict:
        pass


class CmdStanPyBackend(IStanBackend):
    CMDSTAN_VERSION = "2.31.0"

    def __init__(self):
        import cmdstanpy

        # this must be set before super.__init__() for load_model to work on Windows
        local_cmdstan = (
            importlib_resources.files("prophet") / "stan_model" / f"cmdstan-{self.CMDSTAN_VERSION}"
        )
        if local_cmdstan.exists():
            cmdstanpy.set_cmdstan_path(str(local_cmdstan))
        super().__init__()

    @staticmethod
    def get_type():
        return StanBackendEnum.CMDSTANPY.name

    def load_model(self):
        import cmdstanpy

        model_file = importlib_resources.files("prophet") / "stan_model" / "prophet_model.bin"
        return cmdstanpy.CmdStanModel(exe_file=str(model_file))

    def fit(self, stan_init, stan_data, **kwargs):
        if "inits" not in kwargs and "init" in kwargs:
            stan_init = self.sanitize_custom_inits(stan_init, kwargs["init"])
            del kwargs["init"]

        inits_list, data_list = self.prepare_data(stan_init, stan_data)
        args = dict(
            data=data_list,
            inits=inits_list,
            algorithm="Newton" if data_list["T"] < 100 else "LBFGS",
            iter=int(1e4),
        )
        args.update(kwargs)

        try:
            self.stan_fit = self.model.optimize(**args)
        except RuntimeError as e:
            # Fall back on Newton
            if not self.newton_fallback or args["algorithm"] == "Newton":
                raise e
            logger.warning("Optimization terminated abnormally. Falling back to Newton.")
            args["algorithm"] = "Newton"
            self.stan_fit = self.model.optimize(**args)
        params = self.stan_to_dict_numpy(
            self.stan_fit.column_names, self.stan_fit.optimized_params_np
        )
        for par in params:
            params[par] = params[par].reshape((1, -1))
        return params

    def sampling(self, stan_init, stan_data, samples, **kwargs) -> dict:
        if "inits" not in kwargs and "init" in kwargs:
            stan_init = self.sanitize_custom_inits(stan_init, kwargs["init"])
            del kwargs["init"]

        inits_list, data_list = self.prepare_data(stan_init, stan_data)
        args = dict(
            data=data_list,
            inits=inits_list,
        )
        if "chains" not in kwargs:
            kwargs["chains"] = 4
        iter_half = samples // 2
        kwargs["iter_sampling"] = iter_half
        if "iter_warmup" not in kwargs:
            kwargs["iter_warmup"] = iter_half
        args.update(kwargs)

        self.stan_fit = self.model.sample(**args)
        res = self.stan_fit.draws()
        (samples, c, columns) = res.shape
        res = res.reshape((samples * c, columns))
        params = self.stan_to_dict_numpy(self.stan_fit.column_names, res)
        return self.simplify_mcmc_results(params)

    @staticmethod
    def prepare_data(init, data) -> Tuple[dict, dict]:
        """Converts np.ndarrays to lists that can be read by cmdstanpy."""
        cmdstanpy_data = {
            "T": data["T"],
            "S": data["S"],
            "K": data["K"],
            "tau": data["tau"],
            "trend_indicator": data["trend_indicator"],
            "y": data["y"].tolist(),
            "t": data["t"].tolist(),
            "cap": data["cap"].tolist(),
            "t_change": data["t_change"].tolist(),
            "s_a": data["s_a"].tolist(),
            "s_m": data["s_m"].tolist(),
            "X": data["X"].to_numpy().tolist(),
            "sigmas": data["sigmas"],
        }

        cmdstanpy_init = {
            "k": init["k"],
            "m": init["m"],
            "delta": init["delta"].tolist(),
            "beta": init["beta"].tolist(),
            "sigma_obs": init["sigma_obs"],
        }
        return (cmdstanpy_init, cmdstanpy_data)

    @staticmethod
    def stan_to_dict_numpy(column_names: Tuple[str, ...], data: np.ndarray):
        output = OrderedDict()

        prev = None

        start = 0
        end = 0
        two_dims = len(data.shape) > 1
        for cname in column_names:
            parsed = cname.split(".") if "." in cname else cname.split("[")
            curr = parsed[0]
            if prev is None:
                prev = curr

            if curr != prev:
                if prev in output:
                    raise RuntimeError("Found repeated column name")
                if two_dims:
                    output[prev] = np.array(data[:, start:end])
                else:
                    output[prev] = np.array(data[start:end])
                prev = curr
                start = end
            end += 1
        if prev in output:
            raise RuntimeError("Found repeated column name")
        if two_dims:
            output[prev] = np.array(data[:, start:end])
        else:
            output[prev] = np.array(data[start:end])
        return output


class NumpyroBackend(IStanBackend):
    def __init__(self):
        try:
            import numpyro
        except ImportError as exc:
            raise Exception("numpyro not found, please reinstall prophet with `pip install prophet[numpyro]`") from exc
        super().__init__()
        self.newton_fallback = False

    @staticmethod
    def get_type():
        return StanBackendEnum.NUMPYRO.name

    def load_model(self) -> None:
        pass

    @staticmethod
    def prepare_data(
        init: Dict[str, Union[float, int, list]], data: Dict[str, Union[float, int, list]]
    ) -> Tuple[dict, dict]:
        import jax.numpy as jnp

        from .numpyro_model import (
            NumpyroModelData,
            NumpyroModelParams,
            construct_changepoint_matrix,
        )

        data = {k: v for k, v in data.items() if k in NumpyroModelData.__annotations__}
        data["A"] = construct_changepoint_matrix(data["t"], data["t_change"])
        for var in data:
            if NumpyroModelData.__annotations__[var] == jnp.ndarray:
                data[var] = jnp.array(data[var])
            else:
                data[var] = float(data[var])

        init = {k: v for k, v in init.items() if k in NumpyroModelParams.__annotations__}
        for var in init:
            if NumpyroModelParams.__annotations__[var] == jnp.ndarray:
                init[var] = jnp.array(init[var])
            else:
                init[var] = float(init[var])
        return init, data

    def fit(self, stan_init, stan_data, **kwargs) -> dict:
        import jax
        from numpyro.infer import SVI, init_to_value
        from numpyro.infer.autoguide import AutoDelta
        from numpyro.infer.elbo import Trace_ELBO
        from numpyro.optim import Adam

        from .numpyro_model import get_model

        if "inits" not in kwargs and "init" in kwargs:
            stan_init = self.sanitize_custom_inits(stan_init, kwargs["init"])
            del kwargs["init"]
        init, data = self.prepare_data(stan_init, stan_data)
        run_params = {
            "num_steps": 10000,
            "progress_bar": False,
            "learning_rate": 0.001,
            "seed": 0,
        }
        run_params.update(kwargs)

        model = get_model(stan_data["trend_indicator"])
        guide = AutoDelta(model, init_loc_fn=init_to_value(values=init))
        optimizer = Adam(run_params["learning_rate"])
        svi = SVI(model, guide, optimizer, loss=Trace_ELBO())
        rng_key = jax.random.PRNGKey(run_params["seed"])
        svi_results = svi.run(
            rng_key=rng_key,
            num_steps=run_params["num_steps"],
            stable_update=True,
            progress_bar=run_params["progress_bar"],
            **data,
        )
        self.stan_fit = svi_results
        self.run_params = run_params
        self.model = model
        return {
            k.replace("_auto_loc", ""): np.array(v).reshape((1, -1))
            for k, v in svi_results.params.items()
        }

    def sampling(self, stan_init, stan_data, samples: int, **kwargs) -> dict:
        import jax
        import jax.numpy as jnp
        from numpyro.infer import MCMC, NUTS, init_to_median

        from .numpyro_model import get_model

        if "inits" not in kwargs and "init" in kwargs:
            stan_init = self.sanitize_custom_inits(stan_init, kwargs["init"])
            del kwargs["init"]
        init, data = self.prepare_data(stan_init, stan_data)
        run_params = {
            "chain_method": "sequential",
            "progress_bar": False,
            "num_chains": 4,
            "seed": 0,
            "max_tree_depth": 10,
        }
        run_params.update(kwargs)
        init = {
            k: jnp.vstack([jnp.array(v) for _ in range(run_params["num_chains"])])
            for k, v in init.items()
        }

        model = get_model(stan_data["trend_indicator"])
        nuts_kernel = NUTS(model, max_tree_depth=run_params["max_tree_depth"])
        mcmc = MCMC(
            nuts_kernel,
            num_warmup=samples // 2,
            num_samples=samples // 2,
            thinning=1,
            num_chains=run_params["num_chains"],
            chain_method=run_params["chain_method"],
            progress_bar=run_params["progress_bar"],
        )
        rng_key = jax.random.PRNGKey(run_params["seed"])
        mcmc.run(rng_key=rng_key, init_params=init, **data)
        self.stan_fit = mcmc
        self.run_params = run_params
        self.model = model
        params = {k: np.array(v) for k, v in mcmc.get_samples(group_by_chain=False).items()}
        return self.simplify_mcmc_results(params)


class StanBackendEnum(Enum):
    CMDSTANPY = CmdStanPyBackend
    NUMPYRO = NumpyroBackend

    @staticmethod
    def get_backend_class(name: str) -> IStanBackend:
        try:
            return StanBackendEnum[name].value
        except KeyError as e:
            raise ValueError(f"Unknown stan backend: {name}") from e
