# Dispatch recovery timer — server installation

These units schedule `dispatch-sweep` (see the main README, "Dispatch recovery sweeper"). They
match the production API unit `fazilabs-messaging.service`: user/group `dev`, working directory
`/home/dev/app/fazilabs-messaging`, `EnvironmentFile=/home/dev/app/fazilabs-messaging/.env`, and the
checkout's `.venv`. If any of those differ on the target host, edit the units to match that
host's API unit before installing. The units contain no secrets; configuration comes only from the
API's `.env`.

Installing is a separate, explicitly approved deployment step. It must run only after the code
containing the per-row claim fix (`feat/dispatch-sweeper-scheduling`) is deployed to the checkout:
scheduling the old batch-claiming sweeper can record messages as `uncertain` while still submitting
them.

## Pre-checks (read-only)

```bash
systemctl cat fazilabs-messaging.service            # confirm User/Group/WorkingDirectory/EnvironmentFile
cd /home/dev/app/fazilabs-messaging && git log -1 --oneline   # confirm the deployed revision
sudo -u dev /home/dev/app/fazilabs-messaging/.venv/bin/python -m app.cli dispatch-sweep --help
```

The pre-check does not run a sweep. `APP_ENVIRONMENT` in the `.env` must be `production`, and
`APP_ALLOW_LIVE_PROVIDER_SENDS` must be unset or `true`; otherwise every scheduled run is refused
and the service shows `failed`.

## Install

```bash
sudo install -m 0644 -o root -g root \
  /home/dev/app/fazilabs-messaging/deployment/systemd/fazilabs-messaging-dispatch-sweep.service \
  /home/dev/app/fazilabs-messaging/deployment/systemd/fazilabs-messaging-dispatch-sweep.timer \
  /etc/systemd/system/
sudo systemd-analyze verify \
  /etc/systemd/system/fazilabs-messaging-dispatch-sweep.service \
  /etc/systemd/system/fazilabs-messaging-dispatch-sweep.timer
sudo systemctl daemon-reload
sudo systemctl start fazilabs-messaging-dispatch-sweep.service     # one supervised pass first
sudo systemctl status fazilabs-messaging-dispatch-sweep.service    # expect status=0/SUCCESS
sudo journalctl -u fazilabs-messaging-dispatch-sweep.service -n 20 # expect dispatch_sweep_completed
sudo systemctl enable --now fazilabs-messaging-dispatch-sweep.timer
```

Only the timer is enabled; the service has no `[Install]` section and is started by the timer.

## Verify

```bash
systemctl list-timers fazilabs-messaging-dispatch-sweep.timer
journalctl -u fazilabs-messaging-dispatch-sweep.service --since "10 minutes ago"
```

Expect one `dispatch_sweep_completed` JSON event per minute. After a reboot, `list-timers` should
show the timer active again with a next run within a minute.

## Pause or roll back

```bash
sudo systemctl disable --now fazilabs-messaging-dispatch-sweep.timer
# full removal:
sudo rm /etc/systemd/system/fazilabs-messaging-dispatch-sweep.{service,timer}
sudo systemctl daemon-reload
```

Stopping the timer only pauses recovery; it never alters messages. The API service is unaffected by
installing, pausing, or removing these units, and does not need a restart.
