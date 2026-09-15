#!/usr/bin/env bash
set -euo pipefail

SHARE_NAME="${SHARE_NAME:-BretterIMGImages}"
SHARE_PATH="${SHARE_PATH:-/home/cbeis/bretter-img/images-store}"
SMB_GROUP="${SMB_GROUP:-bretter-img}"
APP_USER="${APP_USER:-${SUDO_USER:-cbeis}}"
SMB_CONF="/etc/samba/smb.conf"
SUDOERS_FILE="/etc/sudoers.d/bretter-img-samba"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi

apt-get update
apt-get install -y samba acl sudo

groupadd -f "${SMB_GROUP}"
usermod -aG "${SMB_GROUP}" "${APP_USER}"

mkdir -p "${SHARE_PATH}"
chgrp "${SMB_GROUP}" "${SHARE_PATH}"
chmod 2775 "${SHARE_PATH}"

parent="${SHARE_PATH}"
while [[ "${parent}" != "/" && "${parent}" != "." ]]; do
  parent="$(dirname "${parent}")"
  [[ "${parent}" == "/" ]] && break
  setfacl -m "g:${SMB_GROUP}:--x" "${parent}"
done

setfacl -m "g:${SMB_GROUP}:rwx" "${SHARE_PATH}"
setfacl -d -m "g:${SMB_GROUP}:rwx" "${SHARE_PATH}"

cp "${SMB_CONF}" "${SMB_CONF}.bak.$(date +%Y%m%d%H%M%S)"
awk -v share="${SHARE_NAME}" '
  BEGIN { skip = 0 }
  $0 ~ "^\\[" share "\\]" { skip = 1; next }
  skip && $0 ~ "^\\[" { skip = 0 }
  !skip { print }
' "${SMB_CONF}" > "${SMB_CONF}.tmp"
cat >> "${SMB_CONF}.tmp" <<EOF

[${SHARE_NAME}]
   path = ${SHARE_PATH}
   browseable = yes
   read only = no
   guest ok = no
   valid users = @${SMB_GROUP}
   force group = ${SMB_GROUP}
   create mask = 0664
   force create mode = 0660
   directory mask = 2775
   force directory mode = 2770
   inherit permissions = yes
EOF
mv "${SMB_CONF}.tmp" "${SMB_CONF}"

GROUPADD="$(command -v groupadd)"
USERADD="$(command -v useradd)"
USERMOD="$(command -v usermod)"
GETENT="$(command -v getent)"
SMBPASSWD="$(command -v smbpasswd)"
cat > "${SUDOERS_FILE}" <<EOF
${APP_USER} ALL=(root) NOPASSWD: ${GROUPADD} -f ${SMB_GROUP}
${APP_USER} ALL=(root) NOPASSWD: ${USERADD} *
${APP_USER} ALL=(root) NOPASSWD: ${USERMOD} *
${APP_USER} ALL=(root) NOPASSWD: ${GETENT} *
${APP_USER} ALL=(root) NOPASSWD: ${SMBPASSWD} *
EOF
chmod 0440 "${SUDOERS_FILE}"
visudo -cf "${SUDOERS_FILE}" >/dev/null

systemctl restart smbd
systemctl enable smbd

echo "SMB share ready: \\\\$(hostname -I | awk '{print $1}')\\${SHARE_NAME}"
echo "Authorized group: ${SMB_GROUP}"
echo "Web server user allowed to provision SMB users: ${APP_USER}"
