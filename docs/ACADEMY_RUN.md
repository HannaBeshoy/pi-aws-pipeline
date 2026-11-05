# Running it in the AWS Academy Learner Lab (the PC as the device)

About 1 hour. This is the step between the simulator and the Raspberry Pi: real AWS, with the PC playing the Pi and simulated logger files. Write what you see into `docs/academy/` as you go (the outputs, a screenshot per step).

## Before

- AWS CLI v2 and Python 3.10+ on the PC; `pip install boto3`
- this repository cloned
- in the Learner Lab: **Start Lab**, wait for the green dot, then **AWS Details → AWS CLI: Show**, and copy the three lines into `~/.aws/credentials` under `[default]`. They expire with the session (about 4 hours): copy them again after a restart.
- `export AWS_REGION=us-east-1`

## 1. Deploy (10 min)

```bash
aws sts get-caller-identity                      # the account works
deploy/deploy.sh sensor --academy                # LabRole for the functions; no IAM user is made
```

The table at the end lists `Bucket`, `Table`, `ApiUrl` and `AlarmTopic`. In the console, look at each one (S3, DynamoDB, Lambda, CloudWatch alarms, SNS) and take screenshots. If CloudFormation refuses a resource, the event list in the CloudFormation console says which and why: note it, it is a Learner Lab limit worth recording.

Optional: subscribe your e-mail to the topic in the SNS console (the lab may block e-mail; then skip it).

## 2. Upload from the PC (15 min)

```bash
python3 device/sim_readings.py /tmp/logs --start "$(date +%Y-%m-%dT%H:00:00)" --minutes 30
python3 device/uploader.py --logs /tmp/logs --spool /tmp/spool --bucket <Bucket> --device pi-01 --tz "$(cat /etc/timezone 2>/dev/null || echo Europe/Budapest)" --flush
aws s3 ls s3://<Bucket>/raw/ --recursive
```

Then in DynamoDB → Tables → `sensor-readings` → Explore items: 30 minute items and one LATEST.

## 3. Read it back (5 min)

```bash
python3 tools/query.py <ApiUrl> pi-01
python3 tools/query.py <ApiUrl> pi-01 <first minute> <last minute>      # times in UTC, YYYY-MM-DDTHH:MM
```

## 4. The failure cases (15 min)

| Test | How | Expected |
| --- | --- | --- |
| network down | `--spool /tmp/spool` with a wrong `AWS_REGION=xx-none-1` (or Wi-Fi off), run twice, then fix and run again | files wait in `/tmp/spool`, then go up in order; the minute counts are still 300 |
| bad data | edit a line in a new log file to temperature 900, upload | a `quarantine/…bad.json` with the reason; the other readings arrive |
| duplicate | copy an object in S3 to a new name under the same prefix (`aws s3 cp s3://…/a.jsonl.gz s3://…/a-copy.jsonl.gz`) | the function runs again, the counts do not change (`duplicate` in the log) |
| temperature alarm | `aws lambda update-function-configuration --function-name sensor-ingest --environment "Variables={TABLE=sensor-readings,ALARM_TEMP_C=25,ALARM_TOPIC_ARN=<AlarmTopic>,RETENTION_DAYS=90}"`, upload | SNS publishes (CloudWatch → SNS metrics, or the e-mail) |
| device silent | stop uploading for 15 minutes | the `sensor-device-silent` alarm goes to ALARM |

## 5. Measure for the cost estimate (5 min)

CloudWatch → Log groups → `/aws/lambda/sensor-ingest`: the `REPORT` lines give the duration and the memory used per file. Put the typical duration into `tools/cost.py` (`lambda_s`), run it again and commit the new `docs/COST.md`.

## 6. Delete it

```bash
deploy/teardown.sh sensor
```

Then **End Lab**. Commit `docs/academy/`, tick the box in the README and change the status line.
