#!/usr/bin/env bash
# Deploy FalkorDB + the API + Caddy (HTTPS) on one small AWS Graviton instance.
# This is how the live demo (https://falkor.fally.online) was created. Needs the AWS CLI with credentials.
#   REGION=us-east-1 DOMAIN=falkor.example.com scripts/aws/deploy.sh
# Point DOMAIN's A record at the printed IP (Caddy gets the TLS certificate on first request).
# Security: only 80/443 are open; FalkorDB (6379) and the API (8000) stay inside Docker; no SSH (use SSM).
set -euo pipefail
REGION="${REGION:-us-east-1}"
DOMAIN="${DOMAIN:?set DOMAIN, e.g. falkor.example.com}"
TYPE="${TYPE:-t4g.small}"
NAME="hackathon-falkordb"
REPO="${REPO:-https://github.com/Vishnu-AIR/fally-memory-falkordb}"
TAGS="ResourceType=instance,Tags=[{Key=Project,Value=$NAME}]"

AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --query Parameter.Value --output text)
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

SG=$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME-web" --vpc-id "$VPC" \
  --description "80/443 only" --query GroupId --output text)
for p in 80 443; do
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" --protocol tcp --port $p --cidr 0.0.0.0/0
done

aws iam create-role --role-name "$NAME-ssm" --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws iam attach-role-policy --role-name "$NAME-ssm" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name "$NAME-ssm" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$NAME-ssm" --role-name "$NAME-ssm"
sleep 10  # the new instance profile needs a moment before EC2 can use it

USERDATA=$(cat <<UD
#!/bin/bash
set -eux
dnf install -y docker git
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
git clone $REPO /opt/app
cd /opt/app
DOMAIN=$DOMAIN docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d --build
sleep 20
docker compose exec -T api python -m scripts.seed
UD
)

ID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
  --security-group-ids "$SG" --iam-instance-profile Name="$NAME-ssm" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}' \
  --user-data "$USERDATA" --tag-specifications "$TAGS" --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID"
IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "instance $ID  ip $IP  -> point $DOMAIN (A record) at $IP, then open https://$DOMAIN"
