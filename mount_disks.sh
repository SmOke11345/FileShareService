#!/bin/bash
for img in /opt/tg-cloud/disks/*.img; do
    [ -f "$img" ] || continue
    user=$(basename "$img" .img)
    mount_dir="/opt/tg-cloud/files/$user"
    mkdir -p "$mount_dir"
    
    if ! mountpoint -q "$mount_dir"; then
        mount -o loop "$img" "$mount_dir"
    fi
done
