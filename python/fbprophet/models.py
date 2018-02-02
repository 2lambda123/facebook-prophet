# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import pickle

# fb-block 1 start
import pkg_resources
# fb-block 1 end
# fb-block 2


def get_prophet_stan_model(model, seasonality):
    """Load compiled Stan model"""
    # fb-block 3
    # fb-block 4 start
    model_file = pkg_resources.resource_filename(
        'fbprophet',
        'stan_models/{}_{}_growth.pkl'.format(model, seasonality),
    )
    # fb-block 4 end
    with open(model_file, 'rb') as f:
        return pickle.load(f)


prophet_stan_models = {
    ('linear', 'additive'): get_prophet_stan_model('linear', 'additive'),
    ('linear', 'multiplicative'): get_prophet_stan_model('linear', 'multiplicative'),
    ('logistic', 'additive'): get_prophet_stan_model('logistic', 'additive'),
    ('logistic', 'multiplicative'): get_prophet_stan_model('logistic', 'multiplicative'),
}
