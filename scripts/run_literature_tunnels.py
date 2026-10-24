"""Reuse the checked tunnel supervisor for the two literature staging ports only."""
import argparse,hashlib,shutil
from pathlib import Path
from run_explorer_tunnel import ssh_command,run


def main():
    p=argparse.ArgumentParser();p.add_argument('--state-directory',type=Path,required=True);args=p.parse_args()
    known=Path('C:/Projects/00_NutriGenomics_auto/outputs/publication/2026-10-22/tailscale-known-hosts')
    command=ssh_command(shutil.which('ssh'),'keunsoo@100.110.101.17',18125,8064,known,hashlib.sha256(known.read_bytes()).hexdigest())
    command[1:1]=['-J','keunsoo@100.93.148.94']
    command[-1:-1]=['-L','127.0.0.1:18123:127.0.0.1:8063']
    run(command,args.state_directory,18125,'nutriomics-composed-candidate-20261022-v3')


if __name__=='__main__':main()
