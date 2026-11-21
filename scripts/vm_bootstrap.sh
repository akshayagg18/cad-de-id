#!/usr/bin/env bash
# Two parts. NOTHING here is run automatically.
#   PART 1 (run on YOUR machine / Cloud Shell, after you approve the spend): create the VM.
#   PART 2 (run ON the VM): install dependencies and run the pipeline in tmux.
# Review every line first. Replace <ZONE>/<PROJECT> only if you must; do not paste tokens into this file.
set -eu
part="${1:-help}"

case "$part" in
part1)
  cat <<'EOF'
# Budget guard first (console): Billing -> Budgets & alerts -> create a $30 budget with alerts at 50 % / 100 %.
gcloud config set project <PROJECT>
gcloud compute instances create deid-vm \
  --zone=us-central1-a --machine-type=n2-standard-8 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=500GB --boot-disk-type=pd-ssd
# copy the project folder up (no private/, input/, out/ exist locally, so nothing secret goes along):
gcloud compute scp --recurse --zone=us-central1-a <path-to-this-repo-folder> deid-vm:~/cad-de-id
gcloud compute ssh --zone=us-central1-a deid-vm
# When idle:  gcloud compute instances stop deid-vm --zone=us-central1-a
# When finished: gcloud compute instances delete deid-vm --zone=us-central1-a   (deletes the disk too)
EOF
  ;;
part2)
  sudo apt-get update -y && sudo apt-get install -y python3-venv python3-pip tmux curl
  cd "$(cd "$(dirname "$0")/.." && pwd)"
  python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
  python -m pytest tests -q
  export WORK="$HOME/work" ATTEMPT=1 JOBS="$(nproc)"
  bash scripts/00_check_env.sh
  cat <<'EOF'

Next (inside tmux so a dropped SSH session does not kill it):
  tmux new -s deid
  . .venv/bin/activate; export WORK=$HOME/work ATTEMPT=1 JOBS=$(nproc)
  # put the official get-input.sh at $WORK/get-input.sh and upload.py at $WORK/upload.py
  bash scripts/10_download.sh
  bash scripts/20_run_pipeline.sh            # add --resume after an interruption
  MACHINA_UPLOAD_TOKEN=<fresh 1-hour token> DEST=gs://.../attempt-1/ bash scripts/40_upload.sh
EOF
  ;;
*)
  echo "usage: bash scripts/vm_bootstrap.sh part1|part2   (part1 only prints commands)";;
esac
