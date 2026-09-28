#!/bin/sh
# Run Valve's own Steam Frame OS, from its recovery image, as an SSH target for
# testing (see README.md). Run on a Linux host or VM with btrfs, as root:
#   frame-image.sh steamframe-oobe-repair-<build>.img.bz2
# It extracts the rootfs-A partition, mounts it read-only, adds a throwaway
# writable layer, and starts the image's own sshd on port 2223 (user steamos,
# password frame-test-pw). systemctl only records what it's asked.
set -e
command -v bzcat >/dev/null && command -v python3 >/dev/null || {
  command -v apt-get >/dev/null && apt-get install -y -qq bzip2 python3 >/dev/null; }
IMG=${1:?recovery .img.bz2}; RAW=${RAW:-$(dirname "$IMG")/frame-rootfs-A.img}
if [ ! -f "$RAW" ]; then
  # Partition 3 (rootfs-A) from the image's GPT: start and size in 512-byte sectors.
  set -- $(bzcat "$IMG" | head -c 1048576 | python3 -c '
import struct,sys; d=sys.stdin.buffer.read(); e=d[1024+2*128:1024+3*128]
a,b=struct.unpack("<QQ",e[32:48]); print(a, b-a+1)')
  bzcat "$IMG" | tail -c +$(( $1 * 512 + 1 )) | head -c $(( $2 * 512 )) > "$RAW"
fi
R=/mnt/frame; O=/var/lib/frame-ovl; M=/srv/frame
mkdir -p $R $O/upper $O/work $M
mountpoint -q $R || mount -o ro -t btrfs "$(losetup -f --show -r "$RAW")" $R
mountpoint -q $M || mount -t overlay overlay -o lowerdir=$R,upperdir=$O/upper,workdir=$O/work $M
for d in proc sys dev dev/pts; do mountpoint -q $M/$d || mount --rbind /$d $M/$d; done
mountpoint -q $M/run || mount -t tmpfs tmpfs $M/run
mountpoint -q $M/tmp || mount -t tmpfs tmpfs $M/tmp
mkdir -p $M/run/sshd $M/home/steamos $M/usr/local/bin
chroot $M chown 1000:1000 /home/steamos
echo 'steamos:frame-test-pw' | chroot $M chpasswd
chroot $M ssh-keygen -A >/dev/null
# No systemd here: systemctl records power requests instead of acting.
printf '#!/bin/sh\necho "systemctl $*" >> /tmp/power-requests.log\n' > $M/usr/local/bin/systemctl
chmod +x $M/usr/local/bin/systemctl
pkill -f "[s]shd -p 2223" 2>/dev/null || true
chroot $M /usr/bin/sshd -p 2223 -E /tmp/sshd.log
echo up
