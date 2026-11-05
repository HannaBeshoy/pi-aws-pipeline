"""The template: it passes cfn-lint, and its IAM policies really are least privilege.

The device user's policy is taken from infra/template.yaml and enforced by moto's IAM
emulation, so these are real allow / deny decisions, not string checks.
"""
import json, shutil, subprocess

import boto3, pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from moto.core import set_initial_no_auth_action_count

from helpers import ROOT, resolve, template

T = template()
R = T["Resources"]
STACK, ACCOUNT = "sensor", "123456789012"
BUCKET = f"{STACK}-{ACCOUNT}-data"
VALUES = {"AWS::StackName": STACK, "AWS::AccountId": ACCOUNT, "AWS::Partition": "aws", "DeviceId": "pi-01",
          "ReadingsTable.Arn": f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{STACK}-readings",
          "AlarmTopic": f"arn:aws:sns:us-east-1:{ACCOUNT}:{STACK}-alarms",
          "IngestLogGroup.Arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/{STACK}-ingest:*",
          "ApiLogGroup.Arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/{STACK}-api:*"}


def statements(resource):
    return [s for p in R[resource]["Properties"]["Policies"] for s in resolve(p["PolicyDocument"], VALUES)["Statement"]]


def test_cfn_lint_passes():
    if not shutil.which("cfn-lint"):
        pytest.skip("cfn-lint not installed")
    r = subprocess.run(["cfn-lint", f"{ROOT}/infra/template.yaml"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("res", ["IngestRole", "ApiRole", "DeviceUser"])
def test_no_wildcard_actions_or_resources(res):
    for s in statements(res):
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert s["Effect"] == "Allow" and all("*" not in a for a in actions), s
        assert s["Resource"] != "*" and not str(s["Resource"]).endswith(":::*"), s


def test_bucket_is_private_encrypted_and_tls_only():
    b = R["DataBucket"]["Properties"]
    assert all(b["PublicAccessBlockConfiguration"].values())
    assert b["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    deny = R["DataBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert deny["Effect"] == "Deny" and deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert R["ApiUrl"]["Properties"]["AuthType"] == "AWS_IAM"        # the API is not public


def test_raw_data_moves_to_a_cheaper_class_and_expires():
    rules = {r["Id"]: r for r in R["DataBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]}
    assert rules["raw-to-glacier-ir-then-expire"]["Transitions"][0] == {"StorageClass": "GLACIER_IR", "TransitionInDays": 30}
    assert R["ReadingsTable"]["Properties"]["TimeToLiveSpecification"] == {"AttributeName": "expires", "Enabled": True}


@set_initial_no_auth_action_count(4)
@mock_aws
def test_device_user_can_only_upload_to_its_own_prefix():
    """Admin setup (4 unauthenticated calls), then every call as the device user is checked by IAM."""
    iam = boto3.client("iam", region_name="us-east-1")
    admin_s3 = boto3.client("s3", region_name="us-east-1")
    admin_s3.create_bucket(Bucket=BUCKET)
    iam.create_user(UserName="pi-01")
    doc = {"Version": "2012-10-17", "Statement": statements("DeviceUser")}
    iam.put_user_policy(UserName="pi-01", PolicyName="upload-only", PolicyDocument=json.dumps(doc))
    key = iam.create_access_key(UserName="pi-01")["AccessKey"]
    dev = boto3.client("s3", region_name="us-east-1", aws_access_key_id=key["AccessKeyId"],
                       aws_secret_access_key=key["SecretAccessKey"])

    dev.put_object(Bucket=BUCKET, Key="raw/device=pi-01/date=2026-11-05/x.jsonl.gz", Body=b"ok")   # allowed

    def denied(call, **kw):
        with pytest.raises(ClientError) as e:
            call(Bucket=BUCKET, **kw)
        return e.value.response["Error"]["Code"] == "AccessDenied"

    assert denied(dev.put_object, Key="raw/device=pi-02/date=2026-11-05/x.jsonl.gz", Body=b"x")   # another device
    assert denied(dev.put_object, Key="quarantine/x.json", Body=b"x")
    assert denied(dev.get_object, Key="raw/device=pi-01/date=2026-11-05/x.jsonl.gz")             # cannot read back
    assert denied(dev.delete_object, Key="raw/device=pi-01/date=2026-11-05/x.jsonl.gz")          # cannot delete
    assert denied(dev.list_objects_v2)


def test_ingest_role_cannot_delete_or_read_the_quarantine():
    acts = {(a, s["Resource"]) for s in statements("IngestRole")
            for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/raw/*") in acts
    assert ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/quarantine/*") in acts
    assert not any(a.startswith(("s3:Delete", "dynamodb:Delete", "dynamodb:Scan")) for a, _ in acts)
    api_acts = {a for s in statements("ApiRole") for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert api_acts == {"dynamodb:GetItem", "dynamodb:Query", "logs:CreateLogStream", "logs:PutLogEvents"}
