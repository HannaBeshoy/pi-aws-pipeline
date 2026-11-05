# Pi to AWS Pipeline

[![CI](https://github.com/HannaBeshoy/pi-aws-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/HannaBeshoy/pi-aws-pipeline/actions/workflows/ci.yml)

**AWS Cloud course project** · University of Debrecen, Faculty of Informatics · Python, Amazon S3, AWS Lambda, Amazon DynamoDB, IAM, CloudFormation, CloudWatch · **tested with an AWS simulator (moto); AWS Academy and Raspberry Pi runs to come**

The cloud end of the Raspberry Pi sensor gateway. The [sensor logger](https://github.com/HannaBeshoy/pi-serial-logger) writes five readings a second to CSV files on the Pi; this project sends them to AWS, checks them, summarises them per minute and serves them back, built and costed the way the AWS Cloud Foundations course teaches.

- [x] `device/uploader.py`: reads the logger's files, uploads complete minutes to S3 as compressed JSON lines; keeps everything in a local spool while the network is down, never uploads twice after a crash, SHA-256 checked uploads
- [x] `lambda/ingest`: triggered by each upload; checks every reading, quarantines bad ones with the reason, merges per-minute summaries into DynamoDB with an exactly-once effect (safe against repeated S3 events and re-uploads, and against two functions writing at once)
- [x] `lambda/api`: newest reading and per-minute tables, behind an IAM-signed function URL; `tools/query.py` calls it
- [x] `infra/template.yaml`: the whole stack in CloudFormation, passes `cfn-lint`; private encrypted bucket, TLS only, lifecycle to Glacier Instant Retrieval, TTL, least-privilege roles, alarms for errors and for a silent device; an AWS Academy mode using LabRole
- [x] **Cost estimate** from list prices and measured file sizes: **about USD 0.92 a month** for one device, and why storing every reading in DynamoDB would cost 12 times as much ([COST.md](docs/COST.md))
- [x] **33 tests pass** with moto: the device code, the functions, the whole chain from log file to API, and IAM decisions actually enforced (the device can upload only to its own prefix)
- [ ] Deployed in the AWS Academy Learner Lab with the PC as the device ([steps](docs/ACADEMY_RUN.md))
- [ ] Lab day: the Raspberry Pi as the device ([test sheet](docs/LAB_DAY.md))

## Architecture

```text
 Raspberry Pi                               AWS (us-east-1)
 ───────────────                            ─────────────────────────────────────────────────────────
 pi-serial-logger ─► CSV files              S3  raw/device=pi-01/date=2026-11-05/*.jsonl.gz
                       │                     │   (private, SSE, TLS only; 30 days → Glacier IR; 1 year)
                 uploader.py ──PutObject───►─┤
                 spool + state                │ ObjectCreated event
                 (offline buffer)             ▼
                                            Lambda ingest ──► DynamoDB  device | M#2026-11-05T19:00 → count, min/mean/max
                                              │   │                     device | LATEST           → newest reading
                                              │   └─► S3 quarantine/ (bad readings + reason)
                                              ├─► SNS: temperature alarm
                                              └─► CloudWatch metrics → alarms: function errors, device silent 15 min
 PC: tools/query.py ──SigV4──► Lambda api (function URL, AWS_IAM) ──► DynamoDB
```

## Design decisions

| Question | Decision | Why |
| --- | --- | --- |
| Where do raw readings live? | S3, gzip JSON lines | 13 million readings a month are 0.11 GB: S3 storage and requests cost cents; DynamoDB would cost dollars |
| What goes in DynamoDB? | one item per device and minute (count, sum, min, max, mean) and one LATEST item | the queries a dashboard needs, at 1/300 of the writes |
| How is a duplicate handled? | each minute item lists the digests of the parts merged into it; writes are conditional on a version number | S3 events can arrive twice and a device can resend after a crash; the result must still count every reading once |
| How does the Pi authenticate? | an IAM user allowed only `s3:PutObject` under `raw/device=<its id>/` | a stolen Pi can write only its own data, not read, list or delete anything; the Lambda also checks that the device in each reading matches the path |
| Who can read the API? | function URL with `AWS_IAM` auth | no public endpoint |
| Region | us-east-1 | the AWS Academy region; for real EU data, eu-central-1 (Frankfurt) |

## Results so far (simulation)

- 30 simulated minutes (9,000 readings) go from the logger's CSV files through the uploader, S3, the ingest function and DynamoDB to the API: 30 minute rows of 300 readings each, nothing lost or counted twice.
- Three offline periods: the files wait in the spool and are sent in order when the network returns, without duplicates.
- Bad input (out of range, missing field, not JSON, another device's name) is quarantined with the reason; the rest of the file is still processed.
- The device user's policy, taken from the template and enforced by moto's IAM emulation: upload to its own prefix allowed; another device's prefix, reading, deleting and listing denied.

## Running it

```bash
pip install -r requirements-dev.txt
python -m pytest -v tests          # 33 tests, no AWS account needed
python tools/cost.py               # the cost tables
python device/sim_readings.py /tmp/logs --minutes 30      # logger-format files to play with
```

Deploy (AWS CLI v2 configured): `deploy/deploy.sh sensor --academy` in the AWS Academy Learner Lab, or `deploy/deploy.sh sensor --email you@example.com` in a normal account; `deploy/teardown.sh sensor` deletes it all. On the Pi: `deploy/sensor-uploader.service`.

## Repository layout

```text
device/        uploader.py (on the Pi), sim_readings.py (logger-format test data)
lambda/        ingest/ and api/ functions
infra/         template.yaml: the CloudFormation stack
deploy/        deploy.sh, teardown.sh, systemd unit for the Pi
tools/         cost.py, query.py
tests/         33 pytest tests with moto
docs/          project guide, cost estimate, AWS Academy steps, lab-day test sheet
```

The [project guide](docs/PROJECT_GUIDE.md) explains each part and links it to the course modules.

## Status

Verified with moto. Next: deploy in the AWS Academy Learner Lab with the PC as the device, then the Raspberry Pi on the December lab day. Part of a Raspberry Pi sensor gateway with [pi-serial-logger](https://github.com/HannaBeshoy/pi-serial-logger) (System Programming) and [pi-gateway-hardening](https://github.com/HannaBeshoy/pi-gateway-hardening) (IT Security).

## Author

Beshoy Hanna · Computer Science Engineering BSc, University of Debrecen · [LinkedIn](https://www.linkedin.com/in/beshoy-hanna-8b52213a0)
