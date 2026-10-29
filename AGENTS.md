# RM27 Tracking

Apply `.agents/docs/engineering.md`. This independent repository owns generic
image acquisition, preprocessing and offline replay for future RK3576 Linux
research. README.md defines interfaces, commands and current limitations.
Do not add target engagement, armor targeting, collision guidance, attack-related
reacquisition, or flight/control outputs. Do not alter other components or the
third-party MaixCAM repository. No commits, remotes or pushes without authorization.

Use `PYTHONPATH=src python -m unittest discover -s tests -v` for host verification.
On the current workstation use `/home/shiuhou/venvs/mujoco/bin/python`.
Separate host replay evidence from hardware validation. Never describe synthetic
frame times or host receipt times as exposure timestamps. Camera configuration
is a request, not proof of sensor support. Hardware access needs explicit scope.
Keep generated media and logs under ignored artifacts/; preserve original inputs.
