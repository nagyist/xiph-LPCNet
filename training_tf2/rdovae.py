#!/usr/bin/python3
'''Copyright (c) 2022 Amazon

   Redistribution and use in source and binary forms, with or without
   modification, are permitted provided that the following conditions
   are met:

   - Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

   - Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
   ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
   LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
   A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE FOUNDATION OR
   CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
   EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
   PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
   PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
   LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
   NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
   SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import math
import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import Input, GRU, Dense, Embedding, Reshape, Concatenate, Lambda, Conv1D, Multiply, Add, Bidirectional, MaxPooling1D, Activation, GaussianNoise, AveragePooling1D, RepeatVector
from tensorflow.compat.v1.keras.layers import CuDNNGRU
from tensorflow.keras import backend as K
from tensorflow.keras.constraints import Constraint
from tensorflow.keras.initializers import Initializer
from tensorflow.keras.callbacks import Callback
from tensorflow.keras.regularizers import l1
import numpy as np
import h5py
from uniform_noise import UniformNoise

class WeightClip(Constraint):
    '''Clips the weights incident to each hidden unit to be inside a range
    '''
    def __init__(self, c=2):
        self.c = c

    def __call__(self, p):
        # Ensure that abs of adjacent weights don't sum to more than 127. Otherwise there's a risk of
        # saturation when implementing dot products with SSSE3 or AVX2.
        return self.c*p/tf.maximum(self.c, tf.repeat(tf.abs(p[:, 1::2])+tf.abs(p[:, 0::2]), 2, axis=1))
        #return K.clip(p, -self.c, self.c)

    def get_config(self):
        return {'name': self.__class__.__name__,
            'c': self.c}

constraint = WeightClip(0.496)

def soft_quantize(x):
    #x = 4*x
    #x = x - (.25/np.math.pi)*tf.math.sin(2*np.math.pi*x)
    #x = x - (.25/np.math.pi)*tf.math.sin(2*np.math.pi*x)
    #x = x - (.25/np.math.pi)*tf.math.sin(2*np.math.pi*x)    
    return x

def noise_quantize(x):
    return soft_quantize(x + (K.random_uniform((128, 16, 80))-.5) )

def hard_quantize(x):
    x = soft_quantize(x)
    quantized = tf.round(x)
    return x + tf.stop_gradient(quantized - x)

def apply_dead_zone(x):
    d = x[1]*.05
    x = x[0]
    y = x - d*tf.math.tanh(x/(.1+d))
    return y

def rate_loss(y_true,y_pred):
    log2_e = 1.4427
    n = y_pred.shape[-1]
    C = n - log2_e*np.math.log(np.math.gamma(n))
    k = K.sum(K.abs(y_pred), axis=-1)
    p = 1.5
    #rate = C + (n-1)*log2_e*tf.math.log((k**p + (n/5)**p)**(1/p))
    rate = C + (n-1)*log2_e*tf.math.log(k + .112*n**2/(n/1.8+k) )
    return K.mean(rate)

eps=1e-6
def safelog2(x):
    log2_e = 1.4427
    return log2_e*tf.math.log(eps+x)

def feat_dist_loss(y_true,y_pred):
    ceps = y_pred[:,:,:18] - y_true[:,:,:18]
    pitch = 2*(y_pred[:,:,18:19] - y_true[:,:,18:19])/(y_true[:,:,18:19] + 2)
    corr = y_pred[:,:,19:] - y_true[:,:,19:]
    pitch_weight = K.square(K.maximum(0., y_true[:,:,19:]+.5))
    return K.mean(K.square(ceps) + 10*(1/18.)*K.abs(pitch)*pitch_weight + (1/18.)*K.square(corr))

def sq1_rate_loss(y_true,y_pred):
    lambda_val = y_pred[:,:,-1]
    y_pred = y_pred[:,:,:-1]
    log2_e = 1.4427
    n = y_pred.shape[-1]//3
    r = (y_pred[:,:,2*n:])
    p0 = (y_pred[:,:,n:2*n])
    p0 = 1-r**(.5+.5*p0)
    y_pred = y_pred[:,:,:n]
    y_pred = soft_quantize(y_pred)

    y0 = K.maximum(0., 1. - K.abs(y_pred))**2
    rate = -y0*safelog2(p0*r**K.abs(y_pred)) - (1-y0)*safelog2(.5*(1-p0)*(1-r)*r**(K.abs(y_pred)-1))
    rate = -safelog2(-.5*tf.math.log(r)*r**K.abs(y_pred))
    rate = -safelog2((1-r)/(1+r)*r**K.abs(y_pred))
    #rate = -safelog2(- tf.math.sinh(.5*tf.math.log(r))* r**K.abs(y_pred) - tf.math.cosh(K.maximum(0., .5 - K.abs(y_pred))*tf.math.log(r)) + 1)
    rate = lambda_val*K.sum(rate, axis=-1)
    return K.mean(rate)

def sq2_rate_loss(y_true,y_pred):
    lambda_val = y_pred[:,:,-1]
    y_pred = y_pred[:,:,:-1]
    log2_e = 1.4427
    n = y_pred.shape[-1]//3
    r = y_pred[:,:,2*n:]
    p0 = y_pred[:,:,n:2*n]
    p0 = 1-r**(.5+.5*p0)
    #theta = K.minimum(1., .5 + 0*p0 - 0.04*tf.math.log(r))
    #p0 = 1-r**theta
    y_pred = tf.round(y_pred[:,:,:n])
    y0 = K.maximum(0., 1. - K.abs(y_pred))**2
    rate = -y0*safelog2(p0*r**K.abs(y_pred)) - (1-y0)*safelog2(.5*(1-p0)*(1-r)*r**(K.abs(y_pred)-1))
    rate = lambda_val*K.sum(rate, axis=-1)
    return K.mean(rate)

def sq_rate_metric(y_true,y_pred):
    lambda_val = y_pred[:,:,-1]
    y_pred = y_pred[:,:,:-1]
    log2_e = 1.4427
    n = y_pred.shape[-1]//3
    r = y_pred[:,:,2*n:]
    p0 = y_pred[:,:,n:2*n]
    p0 = 1-r**(.5+.5*p0)
    #theta = K.minimum(1., .5 + 0*p0 - 0.04*tf.math.log(r))
    #p0 = 1-r**theta
    y_pred = tf.round(y_pred[:,:,:n])
    y0 = K.maximum(0., 1. - K.abs(y_pred))**2
    rate = -y0*safelog2(p0*r**K.abs(y_pred)) - (1-y0)*safelog2(.5*(1-p0)*(1-r)*r**(K.abs(y_pred)-1))
    rate = K.sum(rate, axis=-1)
    return K.mean(rate)

def var_repeat(x):
    return RepeatVector(K.shape(x[1])[1])(x[0])

def new_rdovae_encoder(nb_used_features=20, nb_bits=17, bunch=4, nb_quant=40, batch_size=128, cond_size=128, cond_size2=256):
    feat = Input(shape=(None, nb_used_features), batch_size=batch_size)

    quant_id = Input(shape=(None,), batch_size=batch_size)
    lambda_val = Input(shape=(None, 1), batch_size=batch_size)
    qembedding = Embedding(nb_quant, 6*nb_bits, name='quant_embed', embeddings_initializer='zeros')
    quant_embed = qembedding(quant_id)
    quant_embed_bunched = AveragePooling1D(pool_size=bunch//2, strides=bunch//2, padding="valid")(quant_embed)

    quant_scale = Activation('softplus')(Lambda(lambda x: x[:,:,:nb_bits], name='quant_scale_embed')(quant_embed_bunched))

    enc_dense1 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='enc_dense1')
    enc_dense2 = CuDNNGRU(cond_size, return_sequences=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='enc_dense2')
    enc_dense3 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='enc_dense3')
    enc_dense4 = CuDNNGRU(cond_size, return_sequences=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='enc_dense4')
    enc_dense5 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='enc_dense5')
    enc_dense6 = CuDNNGRU(cond_size, return_sequences=True, return_state=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='enc_dense6')
    enc_dense7 = Dense(cond_size, activation='tanh', kernel_constraint=constraint, name='enc_dense7')
    enc_dense8 = Dense(cond_size, activation='tanh', kernel_constraint=constraint, name='enc_dense8')

    #bits_dense = Dense(nb_bits, activation='linear', name='bits_dense')
    bits_dense = Conv1D(nb_bits, 4, padding='causal', activation='linear', name='bits_dense')

    zero_out = Lambda(lambda x: 0*x)
    inputs = Concatenate()([Reshape((-1, 2*nb_used_features))(feat), tf.stop_gradient(quant_embed), lambda_val])
    #inputs = Concatenate()([feat, tf.stop_gradient(quant_embed), lambda_val])
    d1 = enc_dense1(inputs)
    d2 = enc_dense2(d1)
    d3 = enc_dense3(d2)
    d4 = enc_dense4(d3)
    d5 = enc_dense5(d4)
    d6, gru_state = enc_dense6(d5)
    d7 = enc_dense7(d6)
    d8 = enc_dense8(d7)
    enc_out = bits_dense(Concatenate()([d1, d2, d3, d4, d5, d6, d7, d8]))
    enc_out = Lambda(lambda x: x[:, bunch//2-1::bunch//2])(enc_out)
    bits = Multiply()([enc_out, quant_scale])
    global_dense1 = Dense(128, activation='tanh', name='gdense1')
    global_dense2 = Dense(16, activation='tanh', name='gdense2')
    global_bits = global_dense2(global_dense1(gru_state))

    encoder = Model([feat, quant_id, lambda_val], [bits, quant_embed_bunched, global_bits], name='encoder')
    return encoder

def new_rdovae_decoder(nb_used_features=20, nb_bits=17, bunch=4, nb_quant=40, batch_size=128, cond_size=128, cond_size2=256):
    bits_input = Input(shape=(None, nb_bits), batch_size=batch_size)
    quant_embed_input = Input(shape=(None, 6*nb_bits), batch_size=batch_size)
    gru_state_input = Input(shape=(16,), batch_size=batch_size)

    
    dec_dense1 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='dec_dense1')
    dec_dense2 = Dense(cond_size, activation='tanh', kernel_constraint=constraint, name='dec_dense2')
    dec_dense3 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='dec_dense3')
    dec_dense4 = CuDNNGRU(cond_size, return_sequences=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='dec_dense4')
    dec_dense5 = CuDNNGRU(cond_size, return_sequences=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='dec_dense5')
    dec_dense6 = CuDNNGRU(cond_size, return_sequences=True, kernel_constraint=constraint, recurrent_constraint=constraint, name='dec_dense6')
    dec_dense7 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='dec_dense7')
    dec_dense8 = Dense(cond_size2, activation='tanh', kernel_constraint=constraint, name='dec_dense8')

    dec_final = Dense(bunch*nb_used_features, activation='linear', name='dec_final')

    div = Lambda(lambda x: x[0]/x[1])
    time_reverse = Lambda(lambda x: K.reverse(x, 1))
    #time_reverse = Lambda(lambda x: x)
    quant_scale_dec = Activation('softplus')(Lambda(lambda x: x[:,:,:nb_bits], name='quant_scale_embed_dec')(quant_embed_input))
    #gru_state_rep = RepeatVector(64//bunch)(gru_state_input)

    gru_state_rep = Lambda(var_repeat, output_shape=(None, 16)) ([gru_state_input, bits_input])

    dec_inputs = Concatenate()([div([bits_input,quant_scale_dec]), tf.stop_gradient(quant_embed_input), gru_state_rep])
    dec1 = dec_dense1(time_reverse(dec_inputs))
    dec2 = dec_dense2(dec1)
    dec3 = dec_dense3(dec2)
    dec4 = dec_dense4(dec3)
    dec5 = dec_dense5(dec4)
    dec6 = dec_dense6(dec5)
    dec7 = dec_dense7(dec6)
    dec8 = dec_dense8(dec7)
    output = Reshape((-1, nb_used_features))(dec_final(Concatenate()([dec1, dec2, dec3, dec4, dec5, dec6, dec7, dec8])))
    decoder = Model([bits_input, quant_embed_input, gru_state_input], time_reverse(output), name='output')
    return decoder


def new_rdovae_model(nb_used_features=20, nb_bits=17, bunch=4, nb_quant=40, batch_size=128, cond_size=128, cond_size2=256):

    feat = Input(shape=(None, nb_used_features), batch_size=batch_size)
    quant_id = Input(shape=(None,), batch_size=batch_size)
    lambda_val = Input(shape=(None, 1), batch_size=batch_size)
    lambda_bunched = AveragePooling1D(pool_size=bunch//2, strides=bunch//2, padding="valid")(lambda_val)

    encoder = new_rdovae_encoder(nb_used_features, nb_bits, bunch, nb_quant, batch_size, cond_size, cond_size2)
    ze, quant_embed_dec, gru_state_dec = encoder([feat, quant_id, lambda_val])

    decoder = new_rdovae_decoder(nb_used_features, nb_bits, bunch, nb_quant, batch_size, cond_size, cond_size2)

    dead_zone = Activation('softplus')(Lambda(lambda x: x[:,:,nb_bits:2*nb_bits], name='dead_zone_embed')(quant_embed_dec))
    soft_distr_embed = Activation('sigmoid')(Lambda(lambda x: x[:,:,2*nb_bits:4*nb_bits], name='soft_distr_embed')(quant_embed_dec))
    hard_distr_embed = Activation('sigmoid')(Lambda(lambda x: x[:,:,4*nb_bits:], name='hard_distr_embed')(quant_embed_dec))

    noisequant = UniformNoise()
    hardquant = Lambda(hard_quantize)
    dzone = Lambda(apply_dead_zone)
    dze = dzone([ze,dead_zone])
    combined_output = decoder([hardquant(dze), tf.stop_gradient(quant_embed_dec), gru_state_dec])
    ndze = noisequant(dze)
    unquantized_output = decoder([ndze, quant_embed_dec, gru_state_dec])
    unquantized_output_dec = decoder([tf.stop_gradient(ndze), tf.stop_gradient(quant_embed_dec), gru_state_dec])

    e2 = Concatenate(name="hard_bits")([dze, hard_distr_embed, lambda_bunched])
    e = Concatenate(name="soft_bits")([dze, soft_distr_embed, lambda_bunched])


    model = Model([feat, quant_id, lambda_val], [combined_output, unquantized_output, unquantized_output_dec, e, e2], name="end2end")
    model.nb_used_features = nb_used_features

    return model, encoder, decoder

