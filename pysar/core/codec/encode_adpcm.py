"""DSP-ADPCM encoder utilities.

This module favors speed over readability and relies on Numba for the hot
paths. The implementation is based on the reference work at:
https://github.com/jackoalan/gc-dspadpcm-encode/
"""

import io

import numpy as np
from numba import njit

from pysar.core.codec.decode_adpcm import PACKET_SAMPLES


@njit(inline='always')
def _get_pcm(pcm_old, pcm_new, idx):
    # Negative indices refer to the previous 14-sample block.
    if idx < 0:
        return float(pcm_old[idx + 14])
    else:
        return float(pcm_new[idx])


@njit
def _inner_product_merge(pcm_old, pcm_new):
    vec = np.zeros(3, dtype=np.float64)
    for i in range(3):
        s = 0.0
        for x in range(14):
            s -= _get_pcm(pcm_old, pcm_new, x - i) * _get_pcm(pcm_old, pcm_new, x)
        vec[i] = s
    return vec


@njit
def _outer_product_merge(pcm_old, pcm_new):
    mtx = np.zeros((3, 3), dtype=np.float64)
    for x in range(1, 3):
        for y in range(1, 3):
            s = 0.0
            for z in range(14):
                s += _get_pcm(pcm_old, pcm_new, z - x) * _get_pcm(pcm_old, pcm_new, z - y)
            mtx[x, y] = s
    return mtx


@njit
def _analyze_ranges(mtx, vecIdxsOut):
    recips = np.zeros(3, dtype=np.float64)
    for x in range(1, 3):
        val = abs(mtx[x, 1])
        if abs(mtx[x, 2]) > val:
            val = abs(mtx[x, 2])
        if val < np.finfo(np.float64).eps:
            return True
        recips[x] = 1.0 / val

    maxIndex = 0
    for i in range(1, 3):
        for x in range(1, i):
            tmp = mtx[x, i]
            for y in range(1, x):
                tmp -= mtx[x, y] * mtx[y, i]
            mtx[x, i] = tmp
        val = 0.0
        for x in range(i, 3):
            tmp = mtx[x, i]
            for y in range(1, i):
                tmp -= mtx[x, y] * mtx[y, i]
            mtx[x, i] = tmp
            tmp_val = abs(tmp) * recips[x]
            if tmp_val >= val:
                val = tmp_val
                maxIndex = x
        if maxIndex != i:
            for y in range(1, 3):
                temp = mtx[maxIndex, y]
                mtx[maxIndex, y] = mtx[i, y]
                mtx[i, y] = temp
            recips[maxIndex] = recips[i]
        vecIdxsOut[i] = maxIndex
        if mtx[i, i] == 0.0:
            return True
        if i != 2:
            factor = 1.0 / mtx[i, i]
            for x in range(i + 1, 3):
                mtx[x, i] *= factor
    min_val = 1.0e10
    max_val = 0.0
    for i in range(1, 3):
        tmp = abs(mtx[i, i])
        if tmp < min_val:
            min_val = tmp
        if tmp > max_val:
            max_val = tmp
    if min_val / max_val < 1.0e-10:
        return True
    return False


@njit
def _bidirectional_filter(mtx, vecIdxs, vec):
    x_val = 0
    for i in range(1, 3):
        index = vecIdxs[i]
        tmp = vec[index]
        vec[index] = vec[i]
        if x_val != 0:
            for y in range(x_val, i):
                tmp -= vec[y] * mtx[i, y]
        elif tmp != 0.0:
            x_val = i
        vec[i] = tmp
    for i in range(2, 0, -1):
        tmp = vec[i]
        for y in range(i + 1, 3):
            tmp -= vec[y] * mtx[i, y]
        vec[i] = tmp / mtx[i, i]
    vec[0] = 1.0


@njit
def _quadratic_merge(vec):
    v2 = vec[2]
    tmp = 1.0 - (v2 * v2)
    if tmp == 0.0:
        return True
    v0 = (vec[0] - (v2 * v2)) / tmp
    v1 = (vec[1] - (vec[1] * v2)) / tmp
    vec[0] = v0
    vec[1] = v1
    return abs(v1) > 1.0


@njit
def _finish_record(v_in, out):
    temp = np.empty(3, dtype=np.float64)
    for z in range(1, 3):
        if v_in[z] >= 1.0:
            temp[z] = 0.9999999999
        elif v_in[z] <= -1.0:
            temp[z] = -0.9999999999
        else:
            temp[z] = v_in[z]
    out[0] = 1.0
    out[1] = (temp[2] * temp[1]) + temp[1]
    out[2] = temp[2]


@njit
def _matrix_filter(src, dst):
    mtx = np.zeros((3, 3), dtype=np.float64)
    mtx[2, 0] = 1.0
    for i in range(1, 3):
        mtx[2, i] = -src[i]

    epsilon = 1e-9
    for i in range(2, 0, -1):
        val = 1.0 - (mtx[i, i] * mtx[i, i])

        if abs(val) < epsilon:
            val = epsilon if val >= 0 else -epsilon

        for y in range(1, i + 1):
            mtx[i - 1, y] = ((mtx[i, i] * mtx[i, y]) + mtx[i, y]) / val
    dst[0] = 1.0
    for i in range(1, 3):
        s = 0.0
        for y in range(1, i + 1):
            s += mtx[i, y] * dst[i - y]
        dst[i] = s


@njit
def _merge_finish_record(src, dst):
    tmp = np.zeros(3, dtype=np.float64)
    val = src[0]
    dst[0] = 1.0
    for i in range(1, 3):
        v2 = 0.0
        for y in range(1, i):
            v2 += dst[y] * src[i - y]
        if val > 0.0:
            dst[i] = -(v2 + src[i]) / val
        else:
            dst[i] = 0.0
        tmp[i] = dst[i]
        for y in range(1, i):
            dst[y] += dst[i] * dst[i - y]
        val *= 1.0 - (dst[i] * dst[i])
    _finish_record(tmp, dst)


@njit
def _contrast_vectors(source1, source2):
    val = (source2[2] * source2[1] - source2[1]) / (1.0 - source2[2] * source2[2])
    val1 = (source1[0] * source1[0]) + (source1[1] * source1[1]) + (source1[2] * source1[2])
    val2 = (source1[0] * source1[1]) + (source1[1] * source1[2])
    val3 = source1[0] * source1[2]
    return val1 + (2.0 * val * val2) + (2.0 * ((-source2[1]) * val + (-source2[2])) * val3)


@njit
def _filter_records(vecBest, exp, records, nRecords):
    # Repeat the filtering process twice, as in the C code.
    for rep in range(2):
        bufferList = np.zeros((exp, 3), dtype=np.float64)
        buffer1 = np.zeros(exp, dtype=np.int32)
        for rec_i in range(nRecords):
            rec = records[rec_i]
            index = 0
            value = 1.0e30
            for i in range(exp):
                tempVal = _contrast_vectors(vecBest[i], rec)
                if tempVal < value:
                    value = tempVal
                    index = i
            buffer1[index] += 1
            buf = np.zeros(3, dtype=np.float64)
            _matrix_filter(rec, buf)
            for j in range(3):
                bufferList[index, j] += buf[j]
        for i in range(exp):
            if buffer1[i] > 0:
                for j in range(3):
                    bufferList[i, j] /= buffer1[i]
        for i in range(exp):
            tmp = np.zeros(3, dtype=np.float64)
            _merge_finish_record(bufferList[i, :], tmp)
            for j in range(3):
                vecBest[i, j] = tmp[j]


@njit
def _dsp_correlate_coefs(pcm_samples):
    samples = pcm_samples.shape[0]
    block_size = 0x3800  # 14336 samples per block
    blockBuffer = np.zeros(block_size, dtype=np.int16)
    pcmHist_old = np.zeros(14, dtype=np.int16)
    pcmHist_new = np.zeros(14, dtype=np.int16)

    # Use a list to store record vectors (each record is a 3-element float64 array)
    records = []
    record_count = 0
    src_idx = 0
    remaining = samples
    while remaining > 0:
        if remaining > block_size:
            frameSamples = block_size
        else:
            frameSamples = remaining
            for z in range(frameSamples, frameSamples + 14):
                if z < block_size:
                    blockBuffer[z] = 0
        for i in range(frameSamples):
            blockBuffer[i] = pcm_samples[src_idx + i]
        src_idx += frameSamples
        remaining -= frameSamples

        i = 0
        while i < frameSamples:
            for z in range(14):
                pcmHist_old[z] = pcmHist_new[z]
            for z in range(14):
                if i + z < frameSamples:
                    pcmHist_new[z] = blockBuffer[i + z]
                else:
                    pcmHist_new[z] = 0
            i += 14

            vec1 = _inner_product_merge(pcmHist_old, pcmHist_new)
            if abs(vec1[0]) > 10.0:
                mtx = _outer_product_merge(pcmHist_old, pcmHist_new)
                vecIdxs = np.zeros(3, dtype=np.int32)
                err = _analyze_ranges(mtx, vecIdxs)
                if not err:
                    _bidirectional_filter(mtx, vecIdxs, vec1)
                    if not _quadratic_merge(vec1):
                        rec = np.zeros(3, dtype=np.float64)
                        _finish_record(vec1, rec)
                        records.append(rec)
                        record_count += 1
    if record_count == 0:
        return np.zeros(16, dtype=np.int16)

    vec1 = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    vecBest = np.zeros((8, 3), dtype=np.float64)

    for rec in records:
        buf = np.zeros(3, dtype=np.float64)
        _matrix_filter(rec, buf)
        vec1[1] += buf[1]
        vec1[2] += buf[2]

    vec1[1] /= record_count
    vec1[2] /= record_count
    tmp0 = np.zeros(3, dtype=np.float64)
    _merge_finish_record(vec1, tmp0)
    for j in range(3):
        vecBest[0, j] = tmp0[j]

    exp = 1
    w = 0
    while w < 3:
        vec2 = np.array([0.0, -1.0, 0.0], dtype=np.float64)
        for i in range(exp):
            for j in range(3):
                vecBest[exp + i, j] = vecBest[i, j] + 0.01 * vec2[j]
        w += 1
        exp = 1 << w
        _filter_records(vecBest, exp, records, record_count)

    coefsOut = np.zeros(16, dtype=np.int16)
    for z in range(8):
        d = -vecBest[z, 1] * 2048.0
        if d > 0:
            if d > 32767.0:
                d = 32767.0
            coefsOut[z * 2] = np.int16(round(d))
        else:
            if d < -32768.0:
                d = -32768.0
            coefsOut[z * 2] = np.int16(round(d))
        d = -vecBest[z, 2] * 2048.0
        if d > 0:
            if d > 32767.0:
                d = 32767.0
            coefsOut[z * 2 + 1] = np.int16(round(d))
        else:
            if d < -32768.0:
                d = -32768.0
            coefsOut[z * 2 + 1] = np.int16(round(d))
    return coefsOut



def dsp_correlate_coefs(source_samples):
    """
    Given a bytes-like object (bytes, bytearray) of raw PCM sample data,
    as obtained by wave.readframes(), this function returns the coefficient
    matrix needed for encoding the sample data to ADPCM.

    :param source_samples: Raw byte buffer of PCM sample data
    """
    pcm_samples = np.array(source_samples, dtype=np.int16)
    result = _dsp_correlate_coefs(pcm_samples)
    return result.reshape((8, 2))



@njit
def _c_div(num, den):
    return int(num / den)


@njit
def dsp_encode_frame(pcmInOut, coefsIn):
    sampleCount = PACKET_SAMPLES

    inSamples = np.zeros((8, 16), dtype=np.int32)
    outSamples = np.zeros((8, 14), dtype=np.int32)

    distAccum = np.zeros(8, dtype=np.float64)
    scale = np.zeros(8, dtype=np.int32)

    # 1) Try each of the 8 possible coef sets
    for i in range(8):
        inSamples[i, 0] = pcmInOut[0]
        inSamples[i, 1] = pcmInOut[1]

        # Find initial distance
        distance = 0
        for s in range(sampleCount):
            # partial prediction
            v1 = _c_div(
                pcmInOut[s]     * coefsIn[i, 1] +
                pcmInOut[s + 1] * coefsIn[i, 0],
                2048
            )
            v2 = pcmInOut[s + 2] - v1
            # clamp
            if v2 > 32767:
                v3 = 32767
            elif v2 < -32768:
                v3 = -32768
            else:
                v3 = v2
            # track largest abs
            if abs(v3) > abs(distance):
                distance = v3

        # Convert that distance to an initial scale guess
        sGuess = 0
        distTemp = distance
        while (sGuess <= 12) and (distTemp > 7 or distTemp < -8):
            sGuess += 1
            distTemp = _c_div(distTemp, 2)

        if sGuess <= 1:
            scale[i] = -1
        else:
            scale[i] = sGuess - 2

        # do-while portion
        while True:
            scale[i] += 1
            if scale[i] > 12:
                scale[i] = 12

            distAccum[i] = 0.0
            index = 0

            # re-init the first 2 samples
            inSamples[i, 0] = pcmInOut[0]
            inSamples[i, 1] = pcmInOut[1]

            for s in range(sampleCount):
                pred = (inSamples[i, s]   * coefsIn[i, 1] +
                        inSamples[i, s+1] * coefsIn[i, 0])
                numer = (pcmInOut[s+2] << 11) - pred
                v2 = _c_div(numer, 2048)

                # rounding to nibble
                if v2 >= 0:
                    v3 = int(v2 / (1 << scale[i]) + 0.4999999)
                else:
                    v3 = int(v2 / (1 << scale[i]) - 0.4999999)

                if v3 < -8:
                    overshoot = -8 - v3
                    if overshoot > index:
                        index = overshoot
                    v3 = -8
                elif v3 > 7:
                    overshoot = v3 - 7
                    if overshoot > index:
                        index = overshoot
                    v3 = 7

                outSamples[i, s] = v3

                rec = pred + ((v3 * (1 << scale[i])) << 11)
                rec += 1024
                rec >>= 11
                if rec > 32767:
                    rec = 32767
                elif rec < -32768:
                    rec = -32768

                inSamples[i, s+2] = rec

                diff = pcmInOut[s+2] - rec
                distAccum[i] += diff*diff

            x = index + 8
            while x > 256:
                scale[i] += 1
                if scale[i] >= 12:
                    scale[i] = 12
                x >>= 1

            if not ((scale[i] < 12) and (index > 1)):
                break

    # Pick best predictor
    bestDist = 1.79769313486231570814527423731704357e+308  # DBL_MAX approx
    bestIndex = 0
    for i in range(8):
        if distAccum[i] < bestDist:
            bestDist = distAccum[i]
            bestIndex = i

    # Overwrite predicted samples
    for s in range(sampleCount):
        pcmInOut[s+2] = np.int16(inSamples[bestIndex, s+2])  # clamp back to int16

    # Build 8-byte output
    adpcmOut = np.zeros(8, dtype=np.uint8)

    firstByte = ((bestIndex & 0xF) << 4) | (scale[bestIndex] & 0xF)
    adpcmOut[0] = firstByte & 0xFF

    # fill leftover outSamples with zeros if needed
    for s in range(sampleCount, 14):
        outSamples[bestIndex, s] = 0

    # pack the nibbles
    for y in range(7):
        nib1 = (outSamples[bestIndex, 2*y]   & 0xF) << 4
        nib2 = (outSamples[bestIndex, 2*y+1] & 0xF)
        adpcmOut[y+1] = (nib1 | nib2) & 0xFF

    return adpcmOut


def dsp_encode(pcm_samples_16, n_samples):
    if n_samples == 0:
        return [0] * 16, b""
    if (
        n_samples > 0
        and len(pcm_samples_16) >= n_samples
        and not any(pcm_samples_16[index] for index in range(n_samples))
    ):
        packet_count = (n_samples + PACKET_SAMPLES - 1) // PACKET_SAMPLES
        return [0] * 16, bytes(packet_count * 8)

    coefs = dsp_correlate_coefs(pcm_samples_16)

    conv_samples = [0] * 16
    packet_count = n_samples // 14 + (n_samples % 14 != 0)

    buffer = io.BytesIO()

    for i in range(packet_count):
        num_samples = min(n_samples - i * PACKET_SAMPLES, PACKET_SAMPLES)
        for s in range(num_samples):
            conv_samples[s + 2] = pcm_samples_16[i * PACKET_SAMPLES + s]

        block = dsp_encode_frame(np.array(conv_samples, dtype=np.int16), coefs)

        conv_samples[0] = conv_samples[14]  # yn1
        conv_samples[1] = conv_samples[15]  # yn2

        buffer.write(bytearray(block))

    return coefs.flatten().tolist(), buffer.getvalue()
