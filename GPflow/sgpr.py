# Copyright 2016 James Hensman, alexggmatthews
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import absolute_import

import numpy as np
import tensorflow as tf

from . import likelihoods
from ._settings import settings
from .mean_functions import Zero
from .model import GPModel
from .param import Param, DataHolder

float_type = settings.dtypes.float_type


class SGPR(GPModel):
    """
    Sparse Variational GP regression. The key reference is

    ::

      @inproceedings{titsias2009variational,
        title={Variational learning of inducing variables in
               sparse Gaussian processes},
        author={Titsias, Michalis K},
        booktitle={International Conference on
                   Artificial Intelligence and Statistics},
        pages={567--574},
        year={2009}
      }



    """

    def __init__(self, X, Y, kern, Z, mean_function=Zero()):
        """
        X is a data matrix, size N x D
        Y is a data matrix, size N x R
        Z is a matrix of pseudo inputs, size M x D
        kern, mean_function are appropriate GPflow objects

        This method only works with a Gaussian likelihood.
        """
        X = DataHolder(X, on_shape_change='pass')
        Y = DataHolder(Y, on_shape_change='pass')
        likelihood = likelihoods.Gaussian()
        GPModel.__init__(self, X, Y, kern, likelihood, mean_function)
        self.Z = Param(Z)
        self.num_data = X.shape[0]
        self.num_latent = Y.shape[1]

    def build_likelihood(self):
        """
        Construct a tensorflow function to compute the bound on the marginal
        likelihood. For a derivation of the terms in here, see the associated
        SGPR notebook.
        """

        num_inducing = tf.shape(self.Z)[0]
        num_data = tf.cast(tf.shape(self.Y)[0], settings.dtypes.float_type)
        output_dim = tf.cast(tf.shape(self.Y)[1], settings.dtypes.float_type)

        err = self.Y - self.mean_function(self.X)
        Kdiag = self.kern.Kdiag(self.X)
        Kuf = self.kern.Kzx(self.Z, self.X)
        Kuu = self.kern.Kzz(self.Z) + tf.eye(num_inducing, dtype=float_type) * settings.numerics.jitter_level
        L = tf.cholesky(Kuu)
        sigma = tf.sqrt(self.likelihood.variance)

        # Compute intermediate matrices
        A = tf.matrix_triangular_solve(L, Kuf, lower=True) / sigma
        AAT = tf.matmul(A, A, transpose_b=True)
        B = AAT + tf.eye(num_inducing, dtype=float_type)
        LB = tf.cholesky(B)
        Aerr = tf.matmul(A, err)
        c = tf.matrix_triangular_solve(LB, Aerr, lower=True) / sigma

        # compute log marginal bound
        bound = -0.5 * num_data * output_dim * np.log(2 * np.pi)
        bound += - output_dim * tf.reduce_sum(tf.log(tf.diag_part(LB)))
        bound -= 0.5 * num_data * output_dim * tf.log(self.likelihood.variance)
        bound += -0.5 * tf.reduce_sum(tf.square(err)) / self.likelihood.variance
        bound += 0.5 * tf.reduce_sum(tf.square(c))
        bound += -0.5 * output_dim * tf.reduce_sum(Kdiag) / self.likelihood.variance
        bound += 0.5 * output_dim * tf.reduce_sum(tf.diag_part(AAT))

        return bound

    def build_predict(self, Xnew, full_cov=False):
        """
        Compute the mean and variance of the latent function at some new points
        Xnew. For a derivation of the terms in here, see the associated SGPR
        notebook.
        """
        num_inducing = tf.shape(self.Z)[0]
        err = self.Y - self.mean_function(self.X)
        Kuf = self.kern.Kzx(self.Z, self.X)
        Kuu = self.kern.Kzz(self.Z) + tf.eye(num_inducing, dtype=float_type) * settings.numerics.jitter_level
        Kus = self.kern.Kzx(self.Z, Xnew)
        sigma = tf.sqrt(self.likelihood.variance)
        L = tf.cholesky(Kuu)
        A = tf.matrix_triangular_solve(L, Kuf, lower=True) / sigma
        B = tf.matmul(A, A, transpose_b=True) + tf.eye(num_inducing, dtype=float_type)
        LB = tf.cholesky(B)
        Aerr = tf.matmul(A, err)
        c = tf.matrix_triangular_solve(LB, Aerr, lower=True) / sigma
        tmp1 = tf.matrix_triangular_solve(L, Kus, lower=True)
        tmp2 = tf.matrix_triangular_solve(LB, tmp1, lower=True)
        mean = tf.matmul(tmp2, c, transpose_a=True)
        if full_cov:
            var = self.kern.K(Xnew) + tf.matmul(tmp2, tmp2, transpose_a=True) \
                  - tf.matmul(tmp1, tmp1, transpose_a=True)
            shape = tf.stack([1, 1, tf.shape(self.Y)[1]])
            var = tf.tile(tf.expand_dims(var, 2), shape)
        else:
            var = self.kern.Kdiag(Xnew) + tf.reduce_sum(tf.square(tmp2), 0) \
                  - tf.reduce_sum(tf.square(tmp1), 0)
            shape = tf.stack([1, tf.shape(self.Y)[1]])
            var = tf.tile(tf.expand_dims(var, 1), shape)
        return mean + self.mean_function(Xnew), var


class GPRFITC(GPModel):
    def __init__(self, X, Y, kern, Z, mean_function=Zero()):
        """
        This implements GP regression with the FITC approximation.
        The key reference is

        @inproceedings{Snelson06sparsegaussian,
        author = {Edward Snelson and Zoubin Ghahramani},
        title = {Sparse Gaussian Processes using Pseudo-inputs},
        booktitle = {Advances In Neural Information Processing Systems },
        year = {2006},
        pages = {1257--1264},
        publisher = {MIT press}
        }

        Implementation loosely based on code from GPML matlab library although
        obviously gradients are automatic in GPflow.

        X is a data matrix, size N x D
        Y is a data matrix, size N x R
        Z is a matrix of pseudo inputs, size M x D
        kern, mean_function are appropriate GPflow objects

        This method only works with a Gaussian likelihood.

        """
        X = DataHolder(X, on_shape_change='pass')
        Y = DataHolder(Y, on_shape_change='pass')
        likelihood = likelihoods.Gaussian()
        GPModel.__init__(self, X, Y, kern, likelihood, mean_function)
        self.Z = Param(Z)
        self.num_data = X.shape[0]
        self.num_latent = Y.shape[1]

    def build_common_terms(self):
        num_inducing = tf.shape(self.Z)[0]
        err = self.Y - self.mean_function(self.X)  # size N x R
        Kdiag = self.kern.Kdiag(self.X)
        Kuf = self.kern.Kzx(self.Z, self.X)
        Kuu = self.kern.Kzz(self.Z) + tf.eye(num_inducing, dtype=float_type) * settings.numerics.jitter_level

        Luu = tf.cholesky(Kuu)  # => Luu Luu^T = Kuu
        V = tf.matrix_triangular_solve(Luu, Kuf)  # => V^T V = Qff = Kuf^T Kuu^-1 Kuf

        diagQff = tf.reduce_sum(tf.square(V), 0)
        nu = Kdiag - diagQff + self.likelihood.variance

        B = tf.eye(num_inducing, dtype=float_type) + tf.matmul(V / nu, V, transpose_b=True)
        L = tf.cholesky(B)
        beta = err / tf.expand_dims(nu, 1)  # size N x R
        alpha = tf.matmul(V, beta)  # size N x R

        gamma = tf.matrix_triangular_solve(L, alpha, lower=True)  # size N x R

        return err, nu, Luu, L, alpha, beta, gamma

    def build_likelihood(self):
        """
        Construct a tensorflow function to compute the bound on the marginal
        likelihood.
        """

        # FITC approximation to the log marginal likelihood is
        # log ( normal( y | mean, K_fitc ) )
        # where K_fitc = Qff + diag( \nu )
        # where Qff = Kfu Kuu^{-1} Kuf
        # with \nu_i = Kff_{i,i} - Qff_{i,i} + \sigma^2

        # We need to compute the Mahalanobis term -0.5* err^T K_fitc^{-1} err
        # (summed over functions).

        # We need to deal with the matrix inverse term.
        # K_fitc^{-1} = ( Qff + \diag( \nu ) )^{-1}
        #            = ( V^T V + \diag( \nu ) )^{-1}
        # Applying the Woodbury identity we obtain
        #            = \diag( \nu^{-1} ) - \diag( \nu^{-1} ) V^T ( I + V \diag( \nu^{-1} ) V^T )^{-1) V \diag(\nu^{-1} )
        # Let \beta =  \diag( \nu^{-1} ) err
        # and let \alpha = V \beta
        # then Mahalanobis term = -0.5* ( \beta^T err - \alpha^T Solve( I + V \diag( \nu^{-1} ) V^T, alpha ) )

        err, nu, Luu, L, alpha, beta, gamma = self.build_common_terms()

        mahalanobisTerm = -0.5 * tf.reduce_sum(tf.square(err) / tf.expand_dims(nu, 1)) \
                          + 0.5 * tf.reduce_sum(tf.square(gamma))

        # We need to compute the log normalizing term -N/2 \log 2 pi - 0.5 \log \det( K_fitc )

        # We need to deal with the log determinant term.
        # \log \det( K_fitc ) = \log \det( Qff + \diag( \nu ) )
        #                    = \log \det( V^T V + \diag( \nu ) )
        # Applying the determinant lemma we obtain
        #                    = \log [ \det \diag( \nu ) \det( I + V \diag( \nu^{-1} ) V^T ) ]
        #                    = \log [ \det \diag( \nu ) ] + \log [ \det( I + V \diag( \nu^{-1} ) V^T ) ]

        constantTerm = -0.5 * self.num_data * tf.log(tf.constant(2. * np.pi, settings.dtypes.float_type))
        logDeterminantTerm = -0.5 * tf.reduce_sum(tf.log(nu)) - tf.reduce_sum(tf.log(tf.diag_part(L)))
        logNormalizingTerm = constantTerm + logDeterminantTerm

        return mahalanobisTerm + logNormalizingTerm * self.num_latent

    def build_predict(self, Xnew, full_cov=False):
        """
        Compute the mean and variance of the latent function at some new points
        Xnew.
        """
        _, _, Luu, L, _, _, gamma = self.build_common_terms()
        Kus = self.kern.Kzx(self.Z, Xnew)  # size  M x Xnew

        w = tf.matrix_triangular_solve(Luu, Kus, lower=True)  # size M x Xnew

        tmp = tf.matrix_triangular_solve(tf.transpose(L), gamma, lower=False)
        mean = tf.matmul(w, tmp, transpose_a=True) + self.mean_function(Xnew)
        intermediateA = tf.matrix_triangular_solve(L, w, lower=True)

        if full_cov:
            var = self.kern.K(Xnew) - tf.matmul(w, w, transpose_a=True) \
                  + tf.matmul(intermediateA, intermediateA, transpose_a=True)
            var = tf.tile(tf.expand_dims(var, 2), tf.stack([1, 1, tf.shape(self.Y)[1]]))
        else:
            var = self.kern.Kdiag(Xnew) - tf.reduce_sum(tf.square(w), 0) \
                  + tf.reduce_sum(tf.square(intermediateA), 0)  # size Xnew,
            var = tf.tile(tf.expand_dims(var, 1), tf.stack([1, tf.shape(self.Y)[1]]))
        return mean, var
