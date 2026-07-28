#!/bin/bash
./start_velo.sh

npm run api  &
npm run dev &

tail -f /dev/null