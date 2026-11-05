# Lab-day test sheet: the Raspberry Pi as the device

About 45 minutes, after the pi-serial-logger and pi-gateway-hardening lab-day steps (the logger runs as the service user `sensor` and writes to `/var/lib/sensor-logs`). Deploy the stack first as in [ACADEMY_RUN.md](ACADEMY_RUN.md).

## 1. Install (10 min)

```bash
sudo apt install python3-boto3
sudo git clone https://github.com/HannaBeshoy/pi-aws-pipeline /opt/pi-aws-pipeline
sudo install -d -m 700 -o sensor -g sensor /etc/sensor-uploader
sudo install -m 600 -o sensor -g sensor ~/credentials /etc/sensor-uploader/credentials   # the lab's credentials, copied over SSH
sudo sed "s/BUCKET/<Bucket>/" /opt/pi-aws-pipeline/deploy/sensor-uploader.service | sudo tee /etc/systemd/system/sensor-uploader.service
sudo systemctl daemon-reload && sudo systemctl enable --now sensor-uploader
journalctl -u sensor-uploader -f
```

The Learner Lab's credentials expire after a few hours: for a longer test, copy them again and `sudo systemctl restart sensor-uploader`. (With a normal AWS account, the template's device user and its access key are used instead.)

## 2. Real data end to end (15 min)

1. Arduino connected, logger running: after 5 to 10 minutes `aws s3 ls s3://<Bucket>/raw/device=pi-01/ --recursive` shows files.
2. `python3 tools/query.py <ApiUrl> pi-01` on the PC: the newest reading matches the Pi's `printf 'LAST\n' | nc localhost 5600` within the upload interval.
3. Warm the LM35 with a finger: the minute's `temp_c_max` rises in the per-minute table.
4. Note the compressed file sizes in S3: real readings compress less than the simulation (cost model: 8.8 bytes a reading). Put the measured value in `docs/COST.md`.

## 3. Unplug the network (10 min)

1. Unplug Ethernet (or turn Wi-Fi off) for 15 minutes; the logger keeps writing.
2. `ls /var/lib/sensor-spool`: files waiting. The `device-silent` alarm goes to ALARM after 15 minutes.
3. Plug back in: the files go up oldest first within the backoff time (at most 15 minutes), the alarm returns to OK, and the per-minute table has no gap and every minute has about 300 readings.

## 4. Power cut (10 min)

1. Pull the Pi's power while it runs. Power it back on.
2. After the next upload, check the minute around the cut: no minute is counted twice (count ≤ 300), and the minute that was cut short has the readings written before the cut.

## Afterwards

- commit `docs/lab_day/` (outputs, screenshots, the measured sizes)
- tick the lab-day box, change the status line, and the CV line to "tested on the Raspberry Pi"
