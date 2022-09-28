
import os
import subprocess
import argparse


import numpy as np
from scipy.io import wavfile
import tensorflow as tf

from rdovae import new_rdovae_model, pvq_quantize, apply_dead_zone, sq_rate_metric
from fec_packets import write_fec_packets, read_fec_packets


debug = False

if debug:
    args = type('dummy', (object,),
    {
        'input' : 'item1.wav',
        'weights' : 'testout/rdovae_alignment_fix_1024_120.h5',
        'enc_lambda' : 0.0007,
        'output' : "test_0007.fec",
        'cond_size' : 1024,
        'num_redundancy_frames' : 64,
        'extra_delay' : 0,
        'dump_data' : './dump_data'
    })()
    os.environ['CUDA_VISIBLE_DEVICES']=""
else:
    parser = argparse.ArgumentParser(description='Encode redundancy for Opus neural FEC. Designed for use with voip application and 20ms frames')

    parser.add_argument('input', metavar='<input signal>', help='audio input (.wav or .raw or .pcm as int16)')
    parser.add_argument('weights', metavar='<weights>', help='trained model file (.h5)')
    parser.add_argument('enc_lambda', metavar='<lambda>', type=float, help='lambda for controlling encoder rate (default=0.0007)', default=0.0007)
    parser.add_argument('output', type=str, help='output file (will be extended with .fec)')

    parser.add_argument('--dump-data', type=str, default='./dump_data', help='path to dump data executable (default ./dump_data)')
    parser.add_argument('--cond-size', metavar='<units>', default=1024, type=int, help='number of units in conditioning network (default 1024)')
    parser.add_argument('--num-redundancy-frames', default=64, type=int, help='number of redundancy frames per packet (default 64)')
    parser.add_argument('--extra-delay', default=0, type=int, help="last features in packet are calculated with the decoder aligned samples, use this option to add extra delay (in samples at 16kHz)")

    parser.add_argument('--debug-output', action='store_true', help='if set, differently assembled features are written to disk')

    args = parser.parse_args()

model, encoder, decoder = new_rdovae_model(nb_used_features=20, nb_bits=80, batch_size=1, cond_size=args.cond_size)
model.load_weights(args.weights)

lpc_order = 16

## prepare input signal
# SILK frame size is 20ms and LPCNet subframes are 10ms
subframe_size = 160
frame_size = 2 * subframe_size

# 91 samples delay to align with SILK decoded frames
silk_delay = 91

# prepend zeros to have enough history to produce the first package
zero_history = (args.num_redundancy_frames - 1) * frame_size

total_delay = silk_delay + zero_history + args.extra_delay

# load signal
if args.input.endswith('.raw') or args.input.endswith('.pcm'):
    signal = np.fromfile(args.input, dtype='int16')
    
elif args.input.endswith('.wav'):
    fs, signal = wavfile.read(args.input)
else:
    raise ValueError(f'unknown input signal format: {args.input}')

# fill up last frame with zeros
padded_signal_length = len(signal) + total_delay
tail = padded_signal_length % frame_size
right_padding = (frame_size - tail) % frame_size
    
signal = np.concatenate((np.zeros(total_delay, dtype=np.int16), signal, np.zeros(right_padding, dtype=np.int16)))

padded_signal_file  = os.path.splitext(args.input)[0] + '_padded.raw'
signal.tofile(padded_signal_file)

# write signal and call dump_data to create features

feature_file = os.path.splitext(args.input)[0] + '_features.f32'
command = f"{args.dump_data} -test {padded_signal_file} {feature_file}"
r = subprocess.run(command, shell=True)
if r.returncode != 0:
    raise RuntimeError(f"command '{command}' failed with exit code {r.returncode}")

# load features
nb_features = model.nb_used_features + lpc_order
nb_used_features = model.nb_used_features

# load features
features = np.fromfile(feature_file, dtype='float32')
num_subframes = len(features) // nb_features
num_subframes = 2 * (num_subframes // 2)
num_frames = num_subframes // 2

features = np.reshape(features, (1, -1, nb_features))
features = features[:, :, :nb_used_features]
features = features[:, :num_subframes, :]

# lambda and q_id (ToDo: check validity of lambda and q_id)
enc_lambda = args.enc_lambda * np.ones((1, num_frames, 1))
quant_id = np.round(10*np.log(enc_lambda/.0007)).astype('int16')


# run encoder
print("running fec encoder...")
symbols, quant_embed_dec, gru_state_dec = encoder.predict([features, quant_id, enc_lambda])

# apply quantization
nsymbols = 80
dead_zone = tf.math.softplus(quant_embed_dec[:, :, nsymbols : 2 * nsymbols])
symbols = apply_dead_zone([symbols, dead_zone]).numpy()
qsymbols = np.round(symbols)
quant_gru_state_dec = pvq_quantize(gru_state_dec, 30)

# rate estimate
hard_distr_embed = tf.math.sigmoid(quant_embed_dec[:, :, 4 * nsymbols : ]).numpy()
rate_input = np.concatenate((symbols, hard_distr_embed, enc_lambda), axis=-1)
rates = sq_rate_metric(None, rate_input, reduce=False).numpy()

# run decoder
input_length = args.num_redundancy_frames // 2
offset = args.num_redundancy_frames - 1

packets = []
packet_sizes = []

for i in range(offset, num_frames):
    print(f"processing frame {i - offset}...")
    features = decoder.predict([symbols[:, i - 2 * input_length + 1 : i + 1 : 2, :], quant_embed_dec[:, :input_length, :], quant_gru_state_dec[:, i, :]])
    packets.append(features)
    packet_size = 8 * int((np.sum(rates[:, i - 2 * input_length + 1 : i + 1 : 2]) + 7) / 8) + 64
    packet_sizes.append(packet_size)


# write packets
packet_file = args.output + '.fec' if not args.output.endswith('.fec') else args.output
write_fec_packets(packet_file, packets, packet_sizes)


print(f"average redundancy rate: {int(round(sum(packet_sizes) / len(packet_sizes) * 50 / 1000))} kbps")


if args.debug_output:
    import itertools

    batches = [2, 4]
    offsets = [0, 4, 20]
    # sanity checks
    # 1. concatenate features at offset 0
    for batch, offset in itertools.product(batches, offsets):

        stop = packets[0].shape[1] - offset
        test_features = np.concatenate([packet[:,stop - batch: stop, :] for packet in packets[::batch//2]], axis=1)

        test_features_full = np.zeros((test_features.shape[1], nb_features), dtype=np.float32)
        test_features_full[:, :nb_used_features] = test_features[0, :, :]

        print(f"writing debug output {packet_file[:-4] + f'_tf_batch{batch}_offset{offset}.f32'}")
        test_features_full.tofile(packet_file[:-4] + f'_tf_batch{batch}_offset{offset}.f32')

