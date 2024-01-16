#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import sys
from pathlib import Path

try:
    from gnuradio import filter as grfilter  # pytype: disable=import-error
    from gnuradio import blocks  # pytype: disable=import-error
    from gnuradio import fft  # pytype: disable=import-error
    from gnuradio import gr  # pytype: disable=import-error
    from gnuradio import zeromq  # pytype: disable=import-error
    from gnuradio.fft import window  # pytype: disable=import-error
except ModuleNotFoundError as err:  # pragma: no cover
    print(
        "Run from outside a supported environment, please run via Docker (https://github.com/IQTLabs/gamutRF#readme): %s"
        % err
    )
    sys.exit(1)

from gamutrf.grsource import get_source
from gamutrf.grinference2mqtt import inference2mqtt
from gamutrf.utils import endianstr


class grscan(gr.top_block):
    def __init__(
        self,
        bucket_range=1.0,
        colormap=16,
        compass=False,
        db_clamp_ceil=50,
        db_clamp_floor=-200,
        dc_block_len=0,
        dc_block_long=False,
        description="",
        external_gps_server="",
        external_gps_server_port=8888,
        fft_batch_size=256,
        fft_processor_affinity=0,
        freq_end=1e9,
        freq_start=100e6,
        gps_server="",
        igain=0,
        inference_addr="0.0.0.0",  # nosec
        inference_min_confidence=0.5,
        inference_min_db=-200,
        inference_model_name="",
        inference_model_server="",
        inference_output_dir="",
        inference_port=8002,
        inference_text_color="",
        iqtlabs=None,
        logaddr="0.0.0.0",  # nosec
        logport=8001,
        low_power_hold_down=False,
        mqtt_server="",
        n_image=0,
        n_inference=0,
        nfft=1024,
        pretune=False,
        rotate_secs=0,
        samp_rate=4.096e6,
        sample_dir="",
        scaling="spectrum",
        sdr="ettus",
        sdrargs=None,
        sigmf=True,
        skip_tune_step=0,
        sweep_sec=30,
        tag_now=False,
        tune_dwell_ms=0,
        tune_step_fft=0,
        tuneoverlap=0.5,
        tuning_ranges="",
        use_external_gps=False,
        use_external_heading=False,
        vkfft=False,
        wavelearner=None,
        write_samples=0,
    ):
        gr.top_block.__init__(self, "scan", catch_exceptions=True)

        tune_step_hz = int(samp_rate * tuneoverlap)
        stare = False

        if freq_end == 0:
            stare = True
            freq_end = freq_start + (tune_step_hz - 1)
            logging.info(
                f"using stare mode, scan from {freq_start/1e6}MHz to {freq_end/1e6}MHz"
            )

        ##################################################
        # Parameters
        ##################################################
        self.freq_end = freq_end
        self.freq_start = freq_start
        self.sweep_sec = sweep_sec
        self.nfft = nfft
        self.wavelearner = wavelearner
        self.iqtlabs = iqtlabs
        self.samp_rate = samp_rate
        self.retune_pre_fft = None
        self.tag_now = tag_now

        ##################################################
        # Blocks
        ##################################################

        logging.info(f"will scan from {freq_start} to {freq_end}")

        self.samples_blocks = []
        if write_samples:
            Path(sample_dir).mkdir(parents=True, exist_ok=True)
            self.samples_blocks.extend(
                [
                    blocks.complex_to_interleaved_short(False, 32767),
                    blocks.stream_to_vector(gr.sizeof_short, nfft * 2),
                    self.iqtlabs.write_freq_samples(
                        "rx_freq",
                        gr.sizeof_short,
                        "_".join(("ci16", endianstr())),
                        nfft * 2,
                        sample_dir,
                        "samples",
                        write_samples,
                        skip_tune_step,
                        int(samp_rate),
                        rotate_secs,
                        igain,
                        sigmf,
                    ),
                ]
            )
        freq_range = freq_end - freq_start
        fft_rate = int(samp_rate / nfft)

        if not tune_step_fft:
            if tune_dwell_ms:
                tune_step_fft = int(fft_rate * (tune_dwell_ms / 1e3))
            else:
                target_retune_hz = freq_range / self.sweep_sec / tune_step_hz
                tune_step_fft = int(fft_rate / target_retune_hz)
                logging.info(
                    f"retuning across {freq_range/1e6}MHz in {self.sweep_sec}s, requires retuning at {target_retune_hz}Hz in {tune_step_hz/1e6}MHz steps ({tune_step_fft} FFTs)"
                )
        if not tune_step_fft:
            logging.info("tune_step_fft cannot be 0 - defaulting to nfft")
            tune_step_fft = nfft
        tune_dwell_ms = tune_step_fft / fft_rate * 1e3
        logging.info(
            f"requested retuning across {freq_range/1e6}MHz every {tune_step_fft} FFTs, dwell time {tune_dwell_ms}ms"
        )
        if stare and tune_dwell_ms > 1e3:
            logging.warn(">1s dwell time in stare mode, updates will be slow!")

        self.sources, cmd_port, self.workaround_start_hook = get_source(
            sdr,
            samp_rate,
            igain,
            nfft,
            tune_step_fft,
            agc=False,
            center_freq=freq_start,
            sdrargs=sdrargs,
        )

        self.fft_blocks = (
            self.get_dc_blocks(dc_block_len, dc_block_long)
            + self.get_fft_blocks(
                vkfft,
                fft_batch_size,
                nfft,
                freq_start,
                freq_end,
                tune_step_hz,
                tune_step_fft,
                skip_tune_step,
                tuning_ranges,
                pretune,
                fft_processor_affinity,
            )
            + self.get_db_blocks(nfft, samp_rate, scaling)
        )
        fft_dir = sample_dir
        if not write_samples:
            fft_dir = ""
        retune_fft = self.iqtlabs.retune_fft(
            "rx_freq",
            nfft,
            nfft,
            int(samp_rate),
            int(freq_start),
            int(freq_end),
            tune_step_hz,
            tune_step_fft,
            skip_tune_step,
            db_clamp_floor,
            db_clamp_ceil,
            fft_dir,
            write_samples,
            bucket_range,
            tuning_ranges,
            description,
            rotate_secs,
            False,
            self.tag_now,
            not pretune and low_power_hold_down,
        )
        self.fft_blocks.append(retune_fft)
        fft_zmq_addr = f"tcp://{logaddr}:{logport}"
        inference_zmq_addr = f"tcp://{inference_addr}:{inference_port}"
        logging.info("serving FFT on %s", fft_zmq_addr)
        self.fft_blocks.append(
            (zeromq.pub_sink(1, 1, fft_zmq_addr, 100, False, 65536, ""))
        )

        self.inference_blocks = []
        if inference_output_dir:
            x = 640
            y = 640
            Path(inference_output_dir).mkdir(parents=True, exist_ok=True)
            self.inference_blocks.extend(
                [
                    self.iqtlabs.image_inference(
                        tag="rx_freq",
                        vlen=nfft,
                        x=x,
                        y=y,
                        image_dir=inference_output_dir,
                        convert_alpha=255,
                        norm_alpha=0,
                        norm_beta=1,
                        norm_type=32,  # cv::NORM_MINMAX = 32
                        colormap=colormap,  # cv::COLORMAP_VIRIDIS = 16, cv::COLORMAP_TURBO = 20,
                        interpolation=1,  # cv::INTER_LINEAR = 1,
                        flip=0,  # 0 means flipping around the x-axis
                        min_peak_points=inference_min_db,
                        model_server=inference_model_server,
                        model_names=inference_model_name,
                        confidence=inference_min_confidence,
                        max_rows=tune_step_fft,
                        rotate_secs=rotate_secs,
                        n_image=n_image,
                        n_inference=n_inference,
                        samp_rate=int(samp_rate),
                        text_color=inference_text_color,
                    )
                ]
            )
            if mqtt_server:
                self.inference_blocks.extend(
                    [
                        inference2mqtt(
                            "inference2mqtt",
                            mqtt_server,
                            compass,
                            gps_server,
                            use_external_gps,
                            use_external_heading,
                            external_gps_server,
                            external_gps_server_port,
                            inference_output_dir,
                        )
                    ]
                )
        if pretune:
            self.msg_connect((self.retune_pre_fft, "tune"), (self.sources[0], cmd_port))
            self.msg_connect((self.retune_pre_fft, "tune"), (retune_fft, "cmd"))
        else:
            self.msg_connect((retune_fft, "tune"), (self.sources[0], cmd_port))
        self.connect_blocks(self.sources[0], self.sources[1:])

        if self.inference_blocks:
            self.connect((retune_fft, 1), (self.inference_blocks[0], 0))
            self.connect_blocks(self.inference_blocks[0], self.inference_blocks[1:])
            self.connect_blocks(
                self.inference_blocks[0],
                [zeromq.pub_sink(1, 1, inference_zmq_addr, 100, False, 65536, "")],
            )
        else:
            self.connect((retune_fft, 1), (blocks.null_sink(gr.sizeof_float * nfft)))

        for pipeline_blocks in (
            self.fft_blocks,
            self.samples_blocks,
        ):
            self.connect_blocks(self.sources[-1], pipeline_blocks)

    def connect_blocks(self, source, other_blocks, last_block_port=0):
        last_block = source
        for block in other_blocks:
            self.connect((last_block, last_block_port), (block, 0))
            last_block = block

    def get_db_blocks(self, nfft, samp_rate, scaling):
        if scaling == "density":
            scale = 1.0 / (samp_rate * sum(self.get_window(nfft)) ** 2)
        elif scaling == "spectrum":
            scale = 1.0 / (sum(self.get_window(nfft)) ** 2)
        else:
            raise ValueError("scaling must be 'spectrum' or 'density'")
        return [
            blocks.complex_to_mag_squared(nfft),
            blocks.multiply_const_ff(scale, nfft),
            blocks.nlog10_ff(10, nfft, 0),
        ]

    def get_window(self, nfft):
        return window.hann(nfft)

    def get_pretune_block(
        self,
        fft_batch_size,
        nfft,
        freq_start,
        freq_end,
        tune_step_hz,
        tune_step_fft,
        skip_tune_step,
        tuning_ranges,
        pretune,
    ):
        # if pretuning, the pretune block will also do the batching.
        if pretune:
            block = self.iqtlabs.retune_pre_fft(
                nfft,
                fft_batch_size,
                "rx_freq",
                int(freq_start),
                int(freq_end),
                tune_step_hz,
                tune_step_fft,
                skip_tune_step,
                tuning_ranges,
                self.tag_now,
            )
        else:
            # otherwise, the pretuning block will just do batching.
            block = blocks.stream_to_vector(gr.sizeof_gr_complex, fft_batch_size * nfft)
        return block

    def apply_window(self, nfft, fft_batch_size):
        window_constants = [val for val in self.get_window(nfft) for _ in range(2)]
        return blocks.multiply_const_vff(window_constants * fft_batch_size)

    def get_offload_fft_blocks(
        self,
        vkfft,
        fft_batch_size,
        nfft,
        fft_processor_affinity,
    ):
        fft_block = None
        fft_roll = False
        if self.wavelearner:
            fft_block = self.wavelearner.fft(int(fft_batch_size * nfft), nfft, True)
            fft_roll = True
        elif vkfft:
            # VkFFT handles batches by using set_multiple_output(), so we do not need
            # to wrap it.
            fft_block = self.iqtlabs.vkfft(fft_batch_size, nfft, True)
            fft_batch_size = 1
        else:
            fft_block = fft.fft_vcc(nfft, True, [], True, 1)
            fft_batch_size = 1
        fft_block.set_thread_priority(99)
        fft_block.set_processor_affinity([fft_processor_affinity])

        fft_blocks = [
            self.apply_window(nfft, fft_batch_size),
            fft_block,
        ]
        if fft_batch_size > 1:
            fft_blocks.append(
                blocks.vector_to_stream(gr.sizeof_gr_complex * nfft, fft_batch_size)
            )
        if fft_roll:
            fft_blocks.append(self.iqtlabs.vector_roll(nfft))
        return fft_batch_size, fft_blocks

    def get_dc_blocks(self, dc_block_len, dc_block_long):
        if dc_block_len:
            return [grfilter.dc_blocker_cc(dc_block_len, dc_block_long)]
        return []

    def get_fft_blocks(
        self,
        vkfft,
        fft_batch_size,
        nfft,
        freq_start,
        freq_end,
        tune_step_hz,
        tune_step_fft,
        skip_tune_step,
        tuning_ranges,
        pretune,
        fft_processor_affinity,
    ):
        fft_batch_size, fft_blocks = self.get_offload_fft_blocks(
            vkfft,
            fft_batch_size,
            nfft,
            fft_processor_affinity,
        )
        self.retune_pre_fft = self.get_pretune_block(
            fft_batch_size,
            nfft,
            freq_start,
            freq_end,
            tune_step_hz,
            tune_step_fft,
            skip_tune_step,
            tuning_ranges,
            pretune,
        )
        return [self.retune_pre_fft] + fft_blocks

    def start(self):
        super().start()
        self.workaround_start_hook(self)
