#!/bin/bash
# Decrypt an iWallpaper (com.macosgame.iwallpaper) video asset.
# Usage: ./decrypt.sh <encrypted_input_file> [output.mp4]
#
# Cipher: AES-128-ECB, PKCS7 padding
# Key:    ASCII "NSData" zero-padded to 16 bytes
set -euo pipefail

IN="${1:?usage: decrypt.sh <encrypted_input> [output.mp4]}"
OUT="${2:-${IN%.bin}.mp4}"

KEY_HEX=$(printf 'NSData' | od -An -tx1 | tr -d ' \n')0000000000000000000000
KEY_HEX=${KEY_HEX:0:32}

openssl enc -aes-128-ecb -d -K "$KEY_HEX" -in "$IN" -out "$OUT"
echo "decrypted -> $OUT  ($(md5 -q "$OUT"))"
