# Dependency notice scope

`third-party-licenses.md` is a snapshot of license metadata from one resolved development environment. It is useful for auditing, but it is not a lock file and is not by itself a complete binary-distribution notice bundle.

- Preserve license and notice files contained in every selected wheel or system package.
- PyTorch was missing from the generated snapshot, so its [upstream top-level BSD-3-Clause license](https://github.com/pytorch/pytorch/blob/main/LICENSE) is copied in `pytorch-BSD-3-Clause.txt`. PyTorch wheels also contain third-party notices that must travel with a redistributed wheel.
- PySide6 exposed license identifiers but no license text to the snapshot generator. A distributor must include and comply with the Qt for Python, Qt, and Shiboken licensing material from the exact wheel set and chosen commercial or open-source license route.
- NumPy, PyTorch, SoundFile, and similar binary wheels can bundle separately licensed native libraries. Their wheel-level notice directories remain authoritative for that resolution.
- ComfyUI, FFmpeg, Qwen3-TTS nodes, and model files are external prerequisites in the source installation. If a release bundles them, add their exact licenses and model terms before distribution.

Release qualification must resolve exact versions per supported platform, regenerate the inventory, and review the resulting license set.
