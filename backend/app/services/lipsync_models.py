"""Phase 13 — network definitions for the local lip-sync engine.

Wav2Lip (Prajwal et al., ACM MM 2020) generator and its lip-sync expert
(SyncNet) discriminator, re-implemented layer-for-layer so the published
checkpoints load with ``strict=True`` (a mismatch raises, which doubles as an
integrity check on the downloaded weights).

Licence note: the Wav2Lip weights are released for research / non-commercial
use only (trained on LRS2). This module contains no weights.
"""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


class Conv2d(nn.Module):
    def __init__(self, cin, cout, kernel_size, stride, padding, residual=False):
        super().__init__()
        self.conv_block = nn.Sequential(nn.Conv2d(cin, cout, kernel_size, stride, padding), nn.BatchNorm2d(cout))
        self.act = nn.ReLU()
        self.residual = residual

    def forward(self, x):
        out = self.conv_block(x)
        if self.residual:
            out = out + x
        return self.act(out)


class Conv2dTranspose(nn.Module):
    def __init__(self, cin, cout, kernel_size, stride, padding, output_padding=0):
        super().__init__()
        self.conv_block = nn.Sequential(
            nn.ConvTranspose2d(cin, cout, kernel_size, stride, padding, output_padding), nn.BatchNorm2d(cout)
        )
        self.act = nn.ReLU()

    def forward(self, x):
        return self.act(self.conv_block(x))


class Wav2Lip(nn.Module):
    def __init__(self):
        super().__init__()
        self.face_encoder_blocks = nn.ModuleList([
            nn.Sequential(Conv2d(6, 16, 7, 1, 3)),
            nn.Sequential(Conv2d(16, 32, 3, 2, 1), Conv2d(32, 32, 3, 1, 1, True), Conv2d(32, 32, 3, 1, 1, True)),
            nn.Sequential(Conv2d(32, 64, 3, 2, 1), Conv2d(64, 64, 3, 1, 1, True), Conv2d(64, 64, 3, 1, 1, True),
                          Conv2d(64, 64, 3, 1, 1, True)),
            nn.Sequential(Conv2d(64, 128, 3, 2, 1), Conv2d(128, 128, 3, 1, 1, True), Conv2d(128, 128, 3, 1, 1, True)),
            nn.Sequential(Conv2d(128, 256, 3, 2, 1), Conv2d(256, 256, 3, 1, 1, True), Conv2d(256, 256, 3, 1, 1, True)),
            nn.Sequential(Conv2d(256, 512, 3, 2, 1), Conv2d(512, 512, 3, 1, 1, True)),
            nn.Sequential(Conv2d(512, 512, 3, 1, 0), Conv2d(512, 512, 1, 1, 0)),
        ])
        self.audio_encoder = nn.Sequential(
            Conv2d(1, 32, 3, 1, 1), Conv2d(32, 32, 3, 1, 1, True), Conv2d(32, 32, 3, 1, 1, True),
            Conv2d(32, 64, 3, (3, 1), 1), Conv2d(64, 64, 3, 1, 1, True), Conv2d(64, 64, 3, 1, 1, True),
            Conv2d(64, 128, 3, 3, 1), Conv2d(128, 128, 3, 1, 1, True), Conv2d(128, 128, 3, 1, 1, True),
            Conv2d(128, 256, 3, (3, 2), 1), Conv2d(256, 256, 3, 1, 1, True),
            Conv2d(256, 512, 3, 1, 0), Conv2d(512, 512, 1, 1, 0),
        )
        self.face_decoder_blocks = nn.ModuleList([
            nn.Sequential(Conv2d(512, 512, 1, 1, 0)),
            nn.Sequential(Conv2dTranspose(1024, 512, 3, 1, 0), Conv2d(512, 512, 3, 1, 1, True)),
            nn.Sequential(Conv2dTranspose(1024, 512, 3, 2, 1, 1), Conv2d(512, 512, 3, 1, 1, True),
                          Conv2d(512, 512, 3, 1, 1, True)),
            nn.Sequential(Conv2dTranspose(768, 384, 3, 2, 1, 1), Conv2d(384, 384, 3, 1, 1, True),
                          Conv2d(384, 384, 3, 1, 1, True)),
            nn.Sequential(Conv2dTranspose(512, 256, 3, 2, 1, 1), Conv2d(256, 256, 3, 1, 1, True),
                          Conv2d(256, 256, 3, 1, 1, True)),
            nn.Sequential(Conv2dTranspose(320, 128, 3, 2, 1, 1), Conv2d(128, 128, 3, 1, 1, True),
                          Conv2d(128, 128, 3, 1, 1, True)),
            nn.Sequential(Conv2dTranspose(160, 64, 3, 2, 1, 1), Conv2d(64, 64, 3, 1, 1, True),
                          Conv2d(64, 64, 3, 1, 1, True)),
        ])
        self.output_block = nn.Sequential(Conv2d(80, 32, 3, 1, 1), nn.Conv2d(32, 3, 1, 1, 0), nn.Sigmoid())

    def forward(self, audio_sequences, face_sequences):
        """audio: (B, 1, 80, 16) mel windows; faces: (B, 6, 96, 96) masked+reference."""
        audio_embedding = self.audio_encoder(audio_sequences)
        feats = []
        x = face_sequences
        for block in self.face_encoder_blocks:
            x = block(x)
            feats.append(x)
        x = audio_embedding
        for block in self.face_decoder_blocks:
            x = block(x)
            x = torch.cat((x, feats[-1]), dim=1)
            feats.pop()
        return self.output_block(x)


class SyncNet(nn.Module):
    """Wav2Lip's lip-sync expert: embeds 5 lower-face frames and 0.2 s of audio."""

    def __init__(self):
        super().__init__()
        self.face_encoder = nn.Sequential(
            Conv2d(15, 32, (7, 7), 1, 3),
            Conv2d(32, 64, 5, (1, 2), 1), Conv2d(64, 64, 3, 1, 1, True), Conv2d(64, 64, 3, 1, 1, True),
            Conv2d(64, 128, 3, 2, 1), Conv2d(128, 128, 3, 1, 1, True), Conv2d(128, 128, 3, 1, 1, True),
            Conv2d(128, 128, 3, 1, 1, True),
            Conv2d(128, 256, 3, 2, 1), Conv2d(256, 256, 3, 1, 1, True), Conv2d(256, 256, 3, 1, 1, True),
            Conv2d(256, 512, 3, 2, 1), Conv2d(512, 512, 3, 1, 1, True), Conv2d(512, 512, 3, 1, 1, True),
            Conv2d(512, 512, 3, 2, 1), Conv2d(512, 512, 3, 1, 0), Conv2d(512, 512, 1, 1, 0),
        )
        self.audio_encoder = nn.Sequential(
            Conv2d(1, 32, 3, 1, 1), Conv2d(32, 32, 3, 1, 1, True), Conv2d(32, 32, 3, 1, 1, True),
            Conv2d(32, 64, 3, (3, 1), 1), Conv2d(64, 64, 3, 1, 1, True), Conv2d(64, 64, 3, 1, 1, True),
            Conv2d(64, 128, 3, 3, 1), Conv2d(128, 128, 3, 1, 1, True), Conv2d(128, 128, 3, 1, 1, True),
            Conv2d(128, 256, 3, (3, 2), 1), Conv2d(256, 256, 3, 1, 1, True), Conv2d(256, 256, 3, 1, 1, True),
            Conv2d(256, 512, 3, 1, 0), Conv2d(512, 512, 1, 1, 0),
        )

    def forward(self, audio_sequences, face_sequences):
        face_embedding = self.face_encoder(face_sequences).view(face_sequences.size(0), -1)
        audio_embedding = self.audio_encoder(audio_sequences).view(audio_sequences.size(0), -1)
        return F.normalize(audio_embedding, p=2, dim=1), F.normalize(face_embedding, p=2, dim=1)


def load_checkpoint(model: nn.Module, path: str) -> nn.Module:
    # weights_only=True refuses pickled code objects (a tampered .pth cannot run code).
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    except Exception:  # noqa: BLE001 — some original checkpoints store extra Python objects
        import numpy as _np

        with torch.serialization.safe_globals([_np.core.multiarray.scalar, _np.dtype]):
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    state = checkpoint.get("state_dict", checkpoint) if isinstance(checkpoint, dict) else checkpoint
    state = {key.replace("module.", "", 1): value for key, value in state.items()}
    model.load_state_dict(state, strict=True)
    return model.eval()
