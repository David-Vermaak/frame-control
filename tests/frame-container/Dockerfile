# A stand-in for the Frame's SSH surface, on Valve's Holo Core aarch64 base (the
# Arch Linux ARM64 port the Frame's SteamOS is built on).
FROM registry.gitlab.steamos.cloud/holo/holo-core-aarch64-preview/base-devel:latest
RUN pacman -Sy --noconfirm --needed openssh sudo python rsync procps-ng && pacman -Scc --noconfirm
# The Frame's user, with a Developer Mode style password and sudo, as on SteamOS.
RUN useradd -m -s /bin/bash steamos && echo 'steamos:frame-test-pw' | chpasswd \
 && echo 'steamos ALL=(ALL) ALL' > /etc/sudoers.d/steamos && chmod 440 /etc/sudoers.d/steamos
# SteamOS's sshd: keys and passwords, no keyboard-interactive.
RUN ssh-keygen -A && printf 'PasswordAuthentication yes\nKbdInteractiveAuthentication no\nUsePAM yes\n' > /etc/ssh/sshd_config.d/10-frame.conf
# No systemd here: a systemctl that records power requests instead of acting.
RUN printf '#!/bin/sh\necho "systemctl $*" >> /tmp/power-requests.log\n' > /usr/local/bin/systemctl && chmod +x /usr/local/bin/systemctl
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
