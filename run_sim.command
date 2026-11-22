#!/bin/bash
# 가상 기체 4대 시뮬레이터 (UDP 14550/14560/14570/14580)
# 다른 대수: ./run_sim.command -n 2 --ports
cd "$(dirname "$0")"
source .venv/bin/activate
if [ $# -eq 0 ]; then
  python sim_multi_vehicle.py -n 4 --ports
else
  python sim_multi_vehicle.py "$@"
fi
