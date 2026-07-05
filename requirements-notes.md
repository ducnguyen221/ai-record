# Dependency notes

## torch / faster-whisper (do NOT clobber)
This project assumes a **working CUDA-enabled `torch`** is already installed
(RTX 4070 reference machine). `requirements.txt` intentionally does **not** pin
or reinstall `torch` — reinstalling can pull a CPU-only wheel and break GPU STT.
Install the other packages first, and only add `torch`/`faster-whisper` if they
are missing, matching your CUDA toolkit.

Install order suggestion:

```
pip install -r requirements.txt          # skips torch (commented out)
# verify torch already works:
python -c "import torch; print(torch.cuda.is_available())"
# only if faster-whisper is missing:
pip install faster-whisper
```

## Model downloads (first run, approximate)
- Whisper `large-v3` (CT2): ~1.5 GB · `medium` ~0.8 GB · `small` ~0.5 GB
- NLLB-200 distilled-600M (CT2 int8, M2): ~1.2–1.5 GB
- Resemblyzer (M3): ~15 MB · ECAPA-TDNN (opt-in): ~80 MB
- pyannote 3.1 (M4, HF-gated): ~30–100 MB

Total first-run download is ~4–6 GB; subsequent runs are offline-capable. The
preflight screen checks cache presence + free disk before recording.

## Tests need none of the above
The unit + integration suites run on CPU with no GPU, no audio hardware, and no
model downloads — heavy libraries are imported lazily and tests inject a
`FakeVad` + `MockTranscriber`. See `requirements-dev.txt`.
