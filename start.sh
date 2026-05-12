#!/data/data/com.termux/files/usr/bin/bash
proot-distro login ubuntu --bind /sdcard:/sdcard -- su username -s /bin/bash -c "cd /root/ynet-vote-research && claude --dangerously-skip-permissions"
