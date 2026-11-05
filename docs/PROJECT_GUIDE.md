# Project guide

How each part works, why it is built that way, and where it sits in the AWS Academy Cloud Foundations modules.

| Module | Where it is used |
| --- | --- |
| 1 Cloud concepts | serverless: no server to run or patch; pay per request |
| 2 Cloud economics and billing | `tools/cost.py`, [COST.md](COST.md): the per-minute design came out of the cost model |
| 3 Global infrastructure | region choice (us-east-1 for the course, eu-central-1 for EU data); S3 and DynamoDB store across several Availability Zones |
| 4 Cloud security | shared responsibility; IAM least privilege; encryption at rest and in transit; no public endpoints |
| 6 Compute | two Lambda functions, event-driven |
| 7 Storage | S3 key layout, storage classes, lifecycle rules |
| 8 Databases | DynamoDB key design, on-demand capacity, TTL, conditional writes |
| 9 Cloud architecture | the Well-Architected pillars, table at the end |
| 10 Automatic scaling and monitoring | Lambda scales with the uploads; CloudWatch metrics and alarms |

## 1. The device: `device/uploader.py`

The logger writes `time,seq,uptime_ms,temp_c,dist_cm,light_pct` lines in local time. The uploader turns each line into a JSON object with the time in UTC (`--tz`, default the Pi's own zone; summer time is handled by `zoneinfo`).

**Complete minutes only.** Readings are grouped by minute; a minute is uploaded when a reading from a later minute has arrived, so in normal operation each minute is in exactly one file. The unfinished minute waits in `pending.jsonl`. At shutdown `--flush` sends it anyway (the systemd unit does this), and the cloud side merges the rest of that minute later.

**The spool.** Readings first go to a file in the spool directory, then the spool is uploaded oldest first. If an upload fails (no network, expired credentials) the run stops there and the files stay; in `--interval` mode the wait doubles after each failure, up to 15 minutes, and returns to normal after a success. The Pi can be offline for days: at 9 bytes a compressed reading, a day is under 4 MB.

**No duplicates after a crash.** Three rules: (1) the read offsets per CSV file are saved only after the spool file is written, (2) every file is written to `.tmp` and renamed, so a crash never leaves half a file, (3) the spool file's name is made from the first and last time in it. If the program dies after writing a spool file but before saving the offsets, the next run reads the same lines and writes the same name again, replacing the file instead of adding a second one. The test `test_a_crash_before_the_offsets_are_saved_does_not_duplicate` does exactly this.

**Half-written lines.** The logger may be in the middle of a line when the uploader reads. Only lines that end in a newline are taken; the offset stops before the incomplete one.

**Integrity.** Each `PutObject` sends a SHA-256 checksum; S3 recomputes it and refuses the upload if they differ.

## 2. S3: the raw data

Key layout: `raw/device=pi-01/date=2026-11-05/pi-01_20261105T190000Z_20261105T190459Z.jsonl.gz`. The `name=value` parts are the partition style Athena and Glue understand, so the raw data can be queried by device and date later without moving it.

| Setting | Value | Why |
| --- | --- | --- |
| Block Public Access | all four on | sensor data is never public |
| Default encryption | SSE-S3 (AES-256) | encryption at rest, no key to manage |
| Bucket policy | deny any request with `aws:SecureTransport` false | encryption in transit |
| Object ownership | bucket owner enforced (ACLs off) | permissions only through IAM and the bucket policy |
| Lifecycle | `raw/`: Glacier Instant Retrieval after 30 days, deleted after 365; `quarantine/`: deleted after 90 | old data is rarely read: Glacier IR stores it for about a sixth of the Standard price and can still be read in milliseconds |

## 3. The ingest function: `lambda/ingest/handler.py`

S3 calls it for every new object under `raw/` ending in `.jsonl.gz`.

**Checking.** Each reading must have the fields, a valid UTC time not in the future, numbers in physical ranges (temperature −40…125 °C, the LM35's range; distance 2…400 cm, the HC-SR04's; light 0…100 %), and the device named in the reading must be the device in the key. Bad readings are written to `quarantine/…bad.json` with the line number and the reason; the good ones in the same file are still used. Nothing is dropped without a trace.

**Summaries.** For each minute the function computes, per quantity, the count, sum, minimum and maximum. These are *mergeable*: two parts of a minute combine exactly (counts and sums add, min of mins, max of maxes), and the mean is sum / count. A missing distance (no echo) is not counted as zero.

**Exactly-once effect.** S3 delivers events *at least* once, so the same object can be processed twice, and a device can upload the same minute again after a crash. Each minute item stores the digests (SHA-256 of the readings' times) of the parts merged into it; a part whose digest is already present is skipped. Two copies of the function may also run at the same moment for the same minute: each item has a version number, and the write is conditional (`version = :read_version`, or `attribute_not_exists` for a new item). The loser gets `ConditionalCheckFailedException`, reads again and retries. This is optimistic locking; the test `test_a_lost_race_is_retried` forces the race.

**LATEST.** One item per device holds the newest reading; it is written only if it is newer than the one there (`ConditionExpression "#o < :o"`), so a file that arrives late cannot move it backwards.

**Alarm and metrics.** A temperature above `ALARM_TEMP_C` publishes one SNS message per file (not one per reading). The function prints one line in CloudWatch's embedded metric format; CloudWatch turns it into the metrics `Readings` and `BadReadings` without any API call.

## 4. DynamoDB: key design

| Partition key `device` | Sort key `sk` | Attributes |
| --- | --- | --- |
| `pi-01` | `LATEST` | the newest reading |
| `pi-01` | `M#2026-11-05T19:00` | `count`, `temp_c {n, sum, min, max, mean}`, `dist_cm {…}`, `light_pct {…}`, `parts`, `version`, `expires` |

All the API needs is one `GetItem` (newest) or one `Query` with `sk BETWEEN "M#from" AND "M#to"` (a time range, already sorted). No scans, no secondary index. On-demand capacity: no capacity to plan, pay per request, and at this traffic cheaper than provisioned. `expires` is the minute plus `RetentionDays` (90) as a Unix time; DynamoDB's TTL deletes old items without write cost.

## 5. The API and the client

`lambda/api/handler.py` answers `?device=pi-01` with the newest reading and `?device=pi-01&from=…&to=…` with one row per minute. Inputs are checked against strict patterns before they reach a query. The function URL uses `AWS_IAM` authentication: every request must be signed with AWS credentials allowed to `lambda:InvokeFunctionUrl`. `tools/query.py` signs with Signature Version 4 using the credentials of the AWS CLI.

## 6. Security: least privilege

| Principal | Allowed | On |
| --- | --- | --- |
| the Pi (IAM user) | `s3:PutObject` | `raw/device=pi-01/*` only |
| ingest function | `s3:GetObject`; `s3:PutObject`; `dynamodb:GetItem, PutItem`; `sns:Publish`; logs | `raw/*`; `quarantine/*`; the table; the topic; its log group |
| api function | `dynamodb:GetItem, Query`; logs | the table; its log group |

No `*` in any action or resource (a test checks), and the device policy is tested for real with moto's IAM emulation. The Pi's key is long-lived, which is the weak point: in production the device would use AWS IoT Core certificates or IAM Roles Anywhere to get short-lived credentials. On the Pi the key file belongs to `sensor` with mode 0600 (the [hardening project](https://github.com/HannaBeshoy/pi-gateway-hardening) sets this user up).

**AWS Academy.** The Learner Lab does not allow creating IAM roles or users. With `LabRoleArn` set (`deploy.sh --academy`) the functions use the lab's `LabRole` and no device user is made; the PC uploads with the lab's temporary credentials. The least-privilege policies stay in the template and are tested with moto, and the guide says which mode was used where.

**Shared responsibility.** AWS secures the hardware, the Lambda runtime and the managed services; this project is responsible for the IAM policies, the bucket settings, encryption choices, the code and the credentials on the Pi.

## 7. Monitoring

| Alarm | Fires when | Why |
| --- | --- | --- |
| `ingest-errors` | the function failed at least once in 5 minutes | a file was not processed (Lambda retries an S3 event twice, then drops it: check the logs and re-run that file) |
| `device-silent` | fewer than 1 reading in each of 3 periods of 5 minutes; missing data counts as breaching | the Pi is off, the network is down or the uploader stopped: the most likely failure of a field device, and the one nothing else would report |

Both send to the SNS topic, with an e-mail subscription if `AlarmEmail` is given. Log groups keep 14 days.

## 8. Infrastructure as code

`infra/template.yaml` creates everything; `deploy/deploy.sh` packages the function folders into a zip in an artifacts bucket and deploys; `deploy/teardown.sh` empties the buckets and deletes the stack (the Learner Lab budget is limited, so delete when not in use). `cfn-lint` checks the template in CI.

Two details: the bucket's event notification needs the function's permission to exist first, and the permission needs the bucket's ARN, a circular reference. It is broken by building the bucket name from the stack name and account (`${AWS::StackName}-${AWS::AccountId}-data`) so the permission can name it before the bucket exists. And the bucket name includes the account number because S3 names are global.

## 9. Well-Architected review

| Pillar | In this project |
| --- | --- |
| Operational excellence | everything in CloudFormation; alarms on errors and silence; structured logs |
| Security | least privilege per principal, private encrypted bucket, TLS only, IAM-authenticated API, input checked at the boundary |
| Reliability | offline spool with backoff; idempotent processing; optimistic locking; managed services across several AZs |
| Performance efficiency | serverless scales with the uploads; key design answers every query with one call |
| Cost optimization | per-minute summaries instead of per-reading items; compression; lifecycle to Glacier IR; TTL; upload interval chosen from the cost model |
| Sustainability | arm64 (Graviton) functions; data that is not needed expires |

## Notes for the three tracks

- **Embedded systems and edge AI.** The device decides what to send: batching, compression and minute alignment happen on the Pi. The same split (raw at the edge, summaries in the cloud) is how fleets of sensors are run.
- **FPGA to semiconductors.** Test benches produce the same shape of data: many measurements per second, summarised per run. The per-minute merge is the same idea as combining partial results from parallel hardware.
- **Product security.** Device identity tied to a data prefix, a check that the device in the data matches the identity, and a stated weak point (long-lived keys) with the production answer.

## Possible extensions

- an Athena table over `raw/` for SQL on the raw readings
- AWS IoT Core instead of an IAM user: certificates on the device, MQTT
- a small web dashboard that calls the API
