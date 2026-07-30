#!/usr/bin/env bash
# =====================================================================
# base-setup.sh — базовая настройка сервера + установка CheevoScope
# Dashboard (CheevoScope), объединено в один непрерывный прогон.
#
# Изначально это была отдельная часть remnakit.sh v2.5.2
# (https://github.com/0xERR404/remnakit, роль "Базовая настройка сервера" /
# пункт меню 1 / REMNAROLE=base) — шаги 1-15. Шаги 16-17 добавлены отдельно,
# чтобы весь процесс (сервер + сам дашборд) проходил одним запуском без
# необходимости вручную запускать второй скрипт.
#
# Единственное, что скрипт НЕ может сделать полностью автоматически — это
# шаг 3: после смены SSH-порта и создания нового пользователя скрипт
# ОСТАНАВЛИВАЕТСЯ и просит вас открыть новое окно терминала и вручную
# проверить вход новым пользователем/портом, прежде чем заблокировать root.
# Это единственная намеренная пауза во всём процессе — без неё легко
# заблокировать себе доступ к серверу (так уже бывало). После вашего
# подтверждения скрипт продолжает работу сам, без остановок, до конца.
#
# Что делает:
#   1. Обновление системы и установка базовых пакетов
#   2. Создание sudo-пользователя
#   3. Смена SSH-порта и блокировка root (здесь пауза на ручную проверку)
#   4. Настройка firewall (UFW)
#   5. PAM-хук уведомления о входе по SSH (no-op, если notify.sh не установлен)
#   6. Fail2ban
#   7. Автообновления безопасности
#   8. Оптимизация сети (BBR)
#   9. Swap 2GB
#   10. Таймзона (dpkg-reconfigure tzdata)
#   11. Скрипт cleanup.sh
#   12. Скрипт healthcheck.sh
#   13. Cron: плановый ребут и healthcheck после ребута
#   14. Регистрация базовой настройки (registry-файл)
#   15. Logrotate для setup.log
#   16. Установка CheevoScope (клонирование, venv, .env, systemd)
#   17. Домен и HTTPS через Caddy (опционально — спрашивает прямо тут же)
#
# Логика (state resume, идемпотентность, откат sshd при битом конфиге,
# блокировка root только после ручного подтверждения входа) — оригинальная,
# без изменений. Урезано (из исходного remnakit.sh): меню других ролей
# (panel/node/ntfy/kuma/gitea), --dry-run, --wait-dns.
#
# Использование:
#   sudo ./base-setup.sh                # интерактивно, через меню вопросов
#   sudo ./base-setup.sh --check        # проверить состояние уже установленного
#   sudo ./base-setup.sh --cleanup      # почистить логи/apt/tmp
#   sudo ./base-setup.sh --debug        # трассировка команд (set -x), кроме секретов
#
# Неинтерактивный запуск (CI/Ansible):
#   sudo REMNANONINTERACTIVE=1 REMNA_NEW_USER=deploy REMNA_SSH_PORT=2222 \
#     REMNA_REBOOT_DAY=0 REMNA_REBOOT_TIME=06:00 \
#     REMNA_NEW_USER_SSH_KEY="ssh-ed25519 AAAA..." REMNA_CONFIRM_SSH_ACCESS=1 \
#     REMNA_STEAM_API_KEY=... REMNA_STEAM_ID=7656119... \
#     REMNA_SETUP_DOMAIN=1 REMNA_DOMAIN=example.ru \
#     REMNA_DASHBOARD_LOGIN=admin REMNA_DASHBOARD_PASSWORD=... \
#     ./base-setup.sh
# =====================================================================
set -uo pipefail

# ---------------------------------------------------------------------
# Версия и пути
# ---------------------------------------------------------------------
SCRIPT_VERSION="2.5.2-base-only"
BASE_DIR="/oxerr404"
SCRIPT_PATH="${BASE_DIR}/base-setup.sh"

CONFIG_DIR="${BASE_DIR}/config"
BIN_DIR="${BASE_DIR}/bin"
REGISTRY_FILE="${CONFIG_DIR}/installed-roles"
STATE_FILE="${BASE_DIR}/state/remnawave-setup-state"
NOTIFY_SCRIPT="${BIN_DIR}/notify.sh"
CLEANUP_SCRIPT="${BIN_DIR}/cleanup.sh"
HEALTHCHECK_SCRIPT="${BIN_DIR}/healthcheck.sh"
LOG_DIR="${BASE_DIR}/logs"
LOG_FILE="${LOG_DIR}/setup.log"

SEP_WIDTH=50
SEP=$(printf '=%.0s' $(seq 1 $SEP_WIDTH))

DEBUG_MODE=false
debug_trace_on()  { [ "$DEBUG_MODE" = true ] && set -x; return 0; }
debug_trace_off() { set +x; return 0; }
REMNANONINTERACTIVE="${REMNANONINTERACTIVE:-0}"

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RESET=$'\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_RESET=''
fi

# =====================================================================
# 1. Общие функции: вывод, лог, подтверждения, валидация
# =====================================================================
center_line() {
    local text="$1" len pad
    len=${#text}
    if (( len >= SEP_WIDTH )); then
        printf '%s\n' "$text"
        return
    fi
    pad=$(( (SEP_WIDTH - len) / 2 ))
    printf '%*s%s\n' "$pad" '' "$text"
}

print_banner() {
    echo "$SEP"
    center_line "Базовая настройка сервера  v${SCRIPT_VERSION}"
    echo "$SEP"
}

print_step() {
    echo "$SEP"
    center_line "$*"
    echo "$SEP"
    log STEP "$*"
}

log() {
    local level="$1"; shift
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    echo "$(date '+%F %T') [$level] $*" >> "$LOG_FILE" 2>/dev/null || true
}
info()  { echo "[INFO]  $*" >&2; log INFO "$*"; }
warn()  { echo "${C_YELLOW}[WARN]  $*${C_RESET}" >&2; log WARN "$*"; }
error() { echo "${C_RED}[ERROR] $*${C_RESET}" >&2; log ERROR "$*"; }
ok()    { echo "${C_GREEN}[OK]    $*${C_RESET}" >&2; log OK "$*"; }

confirm_yn() {
    # confirm_yn "Вопрос" [y|n по умолчанию] [ИМЯ_ENV_VAR для неинтерактивного режима]
    local prompt="$1" default="${2:-n}" envvar="${3:-}" answer
    if [ "${REMNANONINTERACTIVE:-0}" = "1" ] && [ -n "$envvar" ]; then
        answer="${!envvar:-}"
        case "${answer,,}" in
            y|yes|1|true) return 0 ;;
            n|no|0|false) return 1 ;;
            "") [ "$default" = "y" ] && return 0 || return 1 ;;
            *) error "Неинтерактивный режим: \$${envvar}=\"${answer}\" не распознано как y/n."; return 1 ;;
        esac
    fi
    while true; do
        if [ "$default" = "y" ]; then
            read -r -p "${prompt} [Y/n]: " answer
            answer="${answer:-y}"
        else
            read -r -p "${prompt} [y/N]: " answer
            answer="${answer:-n}"
        fi
        case "${answer,,}" in
            y|yes|д|да) return 0 ;;
            n|no|н|нет) return 1 ;;
            *) echo "Введите y или n." >&2 ;;
        esac
    done
}

ask_value() {
    # ask_value "Промпт" validator_func_name [ИМЯ_ENV_VAR для неинтерактивного режима]
    # -> печатает валидное значение в stdout
    local prompt="$1" validator="$2" envvar="${3:-}" value
    if [ "${REMNANONINTERACTIVE:-0}" = "1" ] && [ -n "$envvar" ]; then
        value="${!envvar:-}"
        if "$validator" "$value"; then
            printf '%s' "$value"
            return 0
        fi
        error "Неинтерактивный режим: \$${envvar}=\"${value}\" не прошло валидацию для «${prompt}»."
        return 1
    fi
    while true; do
        if [ ! -t 0 ]; then
            error "Неинтерактивный запуск: нужен ответ на вопрос «${prompt}», а stdin не терминал. Прерываю, чтобы не зависнуть в бесконечном цикле."
            return 1
        fi
        read -r -p "${prompt}: " value
        if "$validator" "$value"; then
            printf '%s' "$value"
            return 0
        fi
        echo "Некорректное значение, попробуйте ещё раз." >&2
    done
}

validate_username()  { [[ "$1" =~ ^[a-z_][a-z0-9_-]*$ ]]; }
validate_port()      { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
validate_ssh_port()  { validate_port "$1" && [ "$1" != "22" ]; }
validate_nonempty()  { [ -n "$1" ]; }
validate_time_hhmm() { [[ "$1" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; }
validate_weekday()   { [[ "$1" =~ ^[0-6]$ ]]; }
validate_steam_key() { [[ "$1" =~ ^[A-Fa-f0-9]{32}$ ]]; }
validate_steamid()   { [[ "$1" =~ ^7656119[0-9]{10}$ ]]; }
# RA_USERNAME/RA_API_KEY: retroachievements.org не документирует строгий
# формат ключа так же явно, как Steam (32 hex-символа) — известно только,
# что это непустая строка без пробелов. Проверяем ровно это, не больше.
validate_ra_username() { [[ "$1" =~ ^[A-Za-z0-9_-]+$ ]]; }
validate_ra_api_key()  { [[ "$1" =~ ^[A-Za-z0-9]+$ ]]; }
validate_domain()    { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]; }

check_root() {
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        error "Запустите скрипт с правами root (через sudo)."
        exit 1
    fi
}

retry() {
    # retry <макс_попыток> <база_задержки_сек> -- команда...
    # Экспоненциальная задержка: base, base*2, base*4, ...
    local max_attempts="$1" base_delay="$2" attempt=1 delay
    shift 2
    while true; do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -ge "$max_attempts" ]; then
            error "Команда '$*' не удалась после ${max_attempts} попыток."
            return 1
        fi
        delay=$(( base_delay * (2 ** (attempt - 1)) ))
        warn "Попытка ${attempt}/${max_attempts} не удалась, повтор через ${delay}с..."
        sleep "$delay"
        attempt=$((attempt + 1))
    done
}

check_internet() {
    curl -fsS -m 5 -o /dev/null https://1.1.1.1 2>/dev/null \
        || curl -fsS -m 5 -o /dev/null https://8.8.8.8 2>/dev/null
}

# =====================================================================
# 2. Реестр установленных ролей — сейчас пишется только "base" в
#    base_step_14_registry, и это чисто исторический след того, что
#    изначально скрипт был частью многоролевого remnakit (там разные роли
#    проверяли registry_has друг у друга). Здесь роль всего одна, поэтому
#    ничего этот файл на самом деле не читает — но оставлен как есть, раз
#    другие инструменты (если вы используете remnakit отдельно) могут на
#    него полагаться.
# =====================================================================
registry_add() {
    mkdir -p "$(dirname "$REGISTRY_FILE")"
    touch "$REGISTRY_FILE"
    chmod 600 "$REGISTRY_FILE"
    grep -qxF "$1" "$REGISTRY_FILE" 2>/dev/null || echo "$1" >> "$REGISTRY_FILE"
}

# =====================================================================
# 3. State (сохранение ответов между шагами / резюме после обрыва)
# =====================================================================
save_param() {
    # Значение хранится в base64 — так любые пробелы/кавычки/кириллица/спецсимволы
    # переживают запись и чтение без хрупких sed/eval-трюков.
    local key="$1" val="$2" encoded tmp_file
    mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
    touch "$STATE_FILE"
    chmod 600 "$STATE_FILE"
    encoded=$(printf '%s' "$val" | base64 -w0)
    if grep -q "^${key}=" "$STATE_FILE" 2>/dev/null; then
        tmp_file=$(mktemp)
        grep -v "^${key}=" "$STATE_FILE" > "$tmp_file" || true
        mv "$tmp_file" "$STATE_FILE"
    fi
    echo "${key}=${encoded}" >> "$STATE_FILE"
    chmod 600 "$STATE_FILE"
}
load_state() {
    local key="$1" line
    [ -f "$STATE_FILE" ] || return 0
    line=$(grep "^${key}=" "$STATE_FILE" 2>/dev/null | tail -n1 | cut -d'=' -f2-)
    [ -n "$line" ] && printf '%s' "$line" | base64 -d 2>/dev/null
    return 0
}
clear_state() {
    rm -f "$STATE_FILE"
}

# =====================================================================
# Уведомления: notify.sh сюда не входит (он часть роли Ntfy в полном
# remnakit) — здесь просто безопасная обёртка, тихий no-op, если его нет.
# =====================================================================
notify_send() {
    if [ -x "$NOTIFY_SCRIPT" ]; then
        "$NOTIFY_SCRIPT" "$@" || true
    fi
}

on_error_trap() {
    local exit_code=$? line_no="${1:-?}"
    error "Непредвиденная ошибка на строке ${line_no} (exit=${exit_code})."
    notify_send "Ошибка base-setup.sh на строке ${line_no} (exit=${exit_code})" 2>/dev/null || true
}

# =====================================================================
# 4. step()-раннер с резюме по индексу шага (использует namerefs bash)
# =====================================================================
run_role_steps() {
    local role="$1" names_var="$2" funcs_var="$3"
    local -n names_ref="$names_var"
    local -n funcs_ref="$funcs_var"
    local start_index=0 saved_role saved_index

    saved_role="$(load_state CURRENT_ROLE)"
    if [ "$saved_role" = "$role" ]; then
        saved_index="$(load_state LAST_STEP_INDEX)"
        if [ -n "$saved_index" ]; then
            if [ "${REMNANONINTERACTIVE:-0}" != "1" ] && [ -t 0 ]; then
                if confirm_yn "Обнаружен незавершённый запуск (шаг $((saved_index+2))/${#names_ref[@]}). Продолжить с этого места?" "y"; then
                    start_index=$((saved_index+1))
                else
                    clear_state
                    save_param CURRENT_ROLE "$role"
                fi
            else
                start_index=$((saved_index+1))
            fi
        fi
    else
        save_param CURRENT_ROLE "$role"
    fi

    local i
    for (( i=start_index; i<${#names_ref[@]}; i++ )); do
        print_step "[$((i+1))/${#names_ref[@]}] ${names_ref[$i]}"
        if ! "${funcs_ref[$i]}"; then
            error "Шаг «${names_ref[$i]}» завершился с ошибкой. Запустите скрипт снова — уже сделанное не переделается."
            return 1
        fi
        save_param LAST_STEP_INDEX "$i"
        if [ "${REMNANONINTERACTIVE:-0}" != "1" ] && [ -t 0 ] && [ $((i+1)) -lt ${#names_ref[@]} ]; then
            read -r -p "Шаг выполнен. Нажмите Enter для продолжения... " _dummy
        fi
    done

    clear_state
    return 0
}

smoke_test_after_install() {
    local label="$1" check_fn="$2" out
    out=$("$check_fn")
    if [ "$out" = "ok" ]; then
        ok "Проверка после установки (${label}): ok."
    else
        warn "Проверка после установки (${label}): ${out}"
    fi
}

# =====================================================================
# 5. Роль BASE
# =====================================================================
get_saved_ssh_port() {
    # Порт SSH, применённый предыдущим запуском Base. Нужен, чтобы при смене
    # SSH_PORT отозвать старое правило firewall, а не плодить открытые порты.
    [ -f "${CONFIG_DIR}/ssh-port.conf" ] || return 1
    grep -m1 '^SSH_PORT="' "${CONFIG_DIR}/ssh-port.conf" 2>/dev/null | sed -E 's/^[^=]*="(.*)"$/\1/'
}

save_ssh_port_conf() {
    mkdir -p "$CONFIG_DIR"
    printf 'SSH_PORT="%s"\n' "$1" > "${CONFIG_DIR}/ssh-port.conf"
    chmod 600 "${CONFIG_DIR}/ssh-port.conf"
}

detect_server_ip() {
    # ifconfig.me иногда лагает/недоступен — пробуем ещё пару независимых
    # публичных IP-echo сервисов, прежде чем откатываться на hostname -I
    # (которая может отдать приватный IP на NAT/многоинтерфейсных серверах,
    # непригодный для публичных DNS-записей).
    echo "Определяю IP-адрес сервера..."
    local url
    for url in "https://ifconfig.me" "https://icanhazip.com" "https://api.ipify.org"; do
        SERVER_IP=$(curl -fsS -4 -m 5 "$url" 2>/dev/null | tr -d '[:space:]')
        [[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && break
    done
    if ! [[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    echo "Определён IP: ${SERVER_IP:-не удалось определить}"
}

gather_base_inputs() {
    if [ "$(load_state CURRENT_ROLE)" = "base" ] && [ -n "$(load_state NEW_USER)" ]; then
        if [ "${REMNANONINTERACTIVE:-0}" != "1" ] && [ -t 0 ] && confirm_yn "Обнаружены сохранённые ответы для базовой настройки сервера. Использовать их?" "y"; then
            NEW_USER="$(load_state NEW_USER)"
            SSH_PORT="$(load_state SSH_PORT)"
            REBOOT_DAY="$(load_state REBOOT_DAY)"
            REBOOT_TIME="$(load_state REBOOT_TIME)"
            SERVER_IP="$(load_state SERVER_IP)"
            NEW_USER_SSH_KEY="$(load_state NEW_USER_SSH_KEY)"
            NEW_USER_PASSWORD=""
            return 0
        fi
        clear_state
    fi

    echo "Настройка сервера — несколько вопросов (Enter не пропускает поле)."
    NEW_USER=$(ask_value "Имя sudo-пользователя (строчные латинские буквы/цифры/-/_, не с цифры)" validate_username REMNA_NEW_USER) || return 1
    SSH_PORT=$(ask_value "Новый порт SSH (не 22)" validate_ssh_port REMNA_SSH_PORT) || return 1
    REBOOT_DAY=$(ask_value "День недели планового ребута (0=Вс..6=Сб)" validate_weekday REMNA_REBOOT_DAY) || return 1
    REBOOT_TIME=$(ask_value "Время планового ребута, ЧЧ:ММ" validate_time_hhmm REMNA_REBOOT_TIME) || return 1

    # NEW_USER_SSH_KEY/NEW_USER_PASSWORD: способ входа для NEW_USER. adduser в
    # интерактивном режиме сам спросит пароль в base_step_02_create_user, но в
    # REMNANONINTERACTIVE=1 интерактивного prompt'а нет и adduser либо зависнет,
    # либо создаст пользователя без валидного пароля — а после блокировки root
    # (base_step_03) на сервер стало бы невозможно зайти вообще. Поэтому здесь
    # явно запрашиваем хотя бы один способ входа заранее.
    NEW_USER_SSH_KEY=""
    NEW_USER_PASSWORD=""
    if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
        NEW_USER_SSH_KEY="${REMNA_NEW_USER_SSH_KEY:-}"
        NEW_USER_PASSWORD="${REMNA_NEW_USER_PASSWORD:-}"
        if [ -z "$NEW_USER_SSH_KEY" ] && [ -z "$NEW_USER_PASSWORD" ]; then
            error "Неинтерактивный режим: задайте \$REMNA_NEW_USER_SSH_KEY (публичный SSH-ключ) и/или \$REMNA_NEW_USER_PASSWORD для ${NEW_USER} — иначе после блокировки root вход на сервер станет невозможен."
            return 1
        fi
    else
        if confirm_yn "Добавить публичный SSH-ключ для ${NEW_USER} (рекомендуется, вместо входа по паролю)?" "y"; then
            NEW_USER_SSH_KEY=$(ask_value "Вставьте публичный ключ целиком (одна строка, ssh-ed25519/ssh-rsa/...)" validate_nonempty) || return 1
        fi
    fi

    detect_server_ip

    save_param CURRENT_ROLE base
    save_param NEW_USER "$NEW_USER"
    save_param SSH_PORT "$SSH_PORT"
    save_param REBOOT_DAY "$REBOOT_DAY"
    save_param REBOOT_TIME "$REBOOT_TIME"
    save_param SERVER_IP "$SERVER_IP"
    save_param NEW_USER_SSH_KEY "$NEW_USER_SSH_KEY"
}

base_step_01_update_system() {
    # DEBIAN_FRONTEND только префиксом (не export) — иначе шаг 10 (tzdata) перестаёт быть интерактивным.
    retry 3 5 env DEBIAN_FRONTEND=noninteractive apt-get update -y || return 1
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
        -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" || return 1
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl wget git nano ufw fail2ban unattended-upgrades tzdata dnsutils python3-venv python3-pip \
        -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" || return 1
}

base_step_02_create_user() {
    if id "$NEW_USER" >/dev/null 2>&1; then
        info "Пользователь ${NEW_USER} уже существует, пропускаю."
    else
        if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
            # Только неинтерактивный режим требует заранее заданного ключа/пароля:
            # adduser здесь не используем — он либо зависнет на запросе пароля
            # (нет tty), либо создаст пользователя без валидного пароля.
            # В интерактивном режиме adduser сам спросит пароль в терминале —
            # ничего дополнительно требовать не нужно.
            if [ -z "${NEW_USER_SSH_KEY:-}" ] && [ -z "${NEW_USER_PASSWORD:-}" ]; then
                error "Ни SSH-ключ, ни пароль для ${NEW_USER} не заданы — вход после блокировки root будет невозможен. Прерываю."
                return 1
            fi
            useradd -m -s /bin/bash "$NEW_USER" || return 1
        else
            adduser "$NEW_USER" || return 1
        fi
        usermod -aG sudo "$NEW_USER" || return 1
    fi

    if [ -n "${NEW_USER_SSH_KEY:-}" ]; then
        local home_dir ssh_dir
        home_dir=$(getent passwd "$NEW_USER" | cut -d: -f6)
        if [ -z "$home_dir" ] || [ ! -d "$home_dir" ]; then
            error "Не удалось определить домашний каталог пользователя ${NEW_USER} для установки SSH-ключа."
            return 1
        fi
        ssh_dir="${home_dir}/.ssh"
        mkdir -p "$ssh_dir"
        grep -qxF "$NEW_USER_SSH_KEY" "${ssh_dir}/authorized_keys" 2>/dev/null || \
            echo "$NEW_USER_SSH_KEY" >> "${ssh_dir}/authorized_keys"
        chmod 700 "$ssh_dir"
        chmod 600 "${ssh_dir}/authorized_keys"
        chown -R "${NEW_USER}:${NEW_USER}" "$ssh_dir"
        info "SSH-ключ добавлен в ${ssh_dir}/authorized_keys."
    fi

    if [ -n "${NEW_USER_PASSWORD:-}" ]; then
        # debug_trace_off/on: пароль не должен светиться в set -x при --debug.
        # here-string (а не "echo ... | chpasswd") — так пароль не появляется
        # в argv отдельного процесса echo и не виден через ps aux, пока пайп жив.
        debug_trace_off
        chpasswd <<< "${NEW_USER}:${NEW_USER_PASSWORD}"
        debug_trace_on
        info "Пароль для ${NEW_USER} установлен."
    fi
}

base_step_03_ssh_hardening() {
    local sshd_config="/etc/ssh/sshd_config"
    cp "$sshd_config" "${sshd_config}.bak.$(date +%s)"

    sed -i -E "s/^#?Port .*/Port ${SSH_PORT}/" "$sshd_config"
    grep -q "^Port ${SSH_PORT}$" "$sshd_config" || echo "Port ${SSH_PORT}" >> "$sshd_config"

    sed -i -E "s/^#?PermitRootLogin .*/PermitRootLogin no/" "$sshd_config"
    grep -q "^PermitRootLogin no$" "$sshd_config" || echo "PermitRootLogin no" >> "$sshd_config"

    local line key
    for line in "MaxAuthTries 3" "ClientAliveInterval 300" "ClientAliveCountMax 2"; do
        key=$(awk '{print $1}' <<< "$line")
        sed -i -E "/^#?${key} /d" "$sshd_config"
        echo "$line" >> "$sshd_config"
    done

    # ДОБАВЛЕНО: многие облачные образы (cloud-init) кладут собственные
    # drop-in конфиги в /etc/ssh/sshd_config.d/*.conf (например
    # 50-cloud-init.conf), которые подключаются В КОНЦЕ основного
    # sshd_config и потому МОГУТ ПЕРЕБИТЬ Port/PermitRootLogin, только что
    # выставленные выше — снаружи это выглядит так, будто скрипт отработал
    # без ошибок, а на деле старый порт/root-логин продолжают действовать
    # (или новый порт вовсе не слушается). Правим такие директивы и в
    # drop-in файлах тоже, с тем же бэкапом на случай отката.
    local dropin_dir="/etc/ssh/sshd_config.d" dropin
    if [ -d "$dropin_dir" ]; then
        for dropin in "$dropin_dir"/*.conf; do
            [ -f "$dropin" ] || continue
            if grep -qE '^\s*#?\s*(Port|PermitRootLogin)\s' "$dropin"; then
                cp "$dropin" "${dropin}.bak.$(date +%s)"
                sed -i -E "s/^\s*#?\s*Port\s+.*/Port ${SSH_PORT}/" "$dropin"
                sed -i -E "s/^\s*#?\s*PermitRootLogin\s+.*/PermitRootLogin no/" "$dropin"
                warn "Найден и поправлен конфликтующий Port/PermitRootLogin в ${dropin} (drop-in переопределяет основной sshd_config, если его не поправить)."
            fi
        done
    fi

    # Ubuntu 22.04+: ssh.socket может перехватывать порт, игнорируя Port из конфига
    if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
        systemctl disable --now ssh.socket 2>/dev/null || true
    fi

    # Проверка синтаксиса/семантики sshd_config ПЕРЕД рестартом — иначе битый
    # конфиг (например, из-за неожиданного совпадения sed) уронит sshd прямо
    # на текущей сессии, ещё до ручной проверки входа новым пользователем ниже.
    # При ошибке откатываемся на бэкап, снятый в начале этой функции, и не трогаем sshd.
    mkdir -p /run/sshd
    chmod 0755 /run/sshd
    local sshd_check_err
    sshd_check_err=$(mktemp)
    if ! sshd -t -f "$sshd_config" 2>"$sshd_check_err"; then
        error "sshd_config не прошёл проверку синтаксиса (sshd -t) — sshd НЕ перезапускаю, чтобы не потерять доступ."
        error "$(cat "$sshd_check_err" 2>/dev/null)"
        cp "${sshd_config}.bak."* "$sshd_config" 2>/dev/null
        rm -f "$sshd_check_err"
        return 1
    fi
    rm -f "$sshd_check_err"

    # Firewall уже активен — открываем новый порт заранее (старый закроется позже в base_step_04_firewall).
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
        ufw limit "${SSH_PORT}/tcp"
    fi

    systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || return 1

    # Рестарт мог "формально" пройти, но sshd не подняться (например, порт занят
    # другим процессом) — без этой проверки скрипт пошёл бы дальше к запросу
    # "успешно зашли новым пользователем?", хотя sshd уже не слушает вовсе.
    sleep 1
    if ! systemctl is-active --quiet ssh 2>/dev/null && ! systemctl is-active --quiet sshd 2>/dev/null; then
        error "sshd не запустился после рестарта — проверьте 'systemctl status ssh'/'journalctl -u ssh'. Конфиг НЕ откатываю автоматически (sshd -t прошёл), разбирайтесь вручную, не закрывая текущую сессию."
        return 1
    fi

    if [ "$(passwd -S root 2>/dev/null | awk '{print $2}')" = "L" ]; then
        info "root уже заблокирован, пропускаю проверку входа."
        return 0
    fi

    if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
        if [ "${REMNA_CONFIRM_SSH_ACCESS:-0}" != "1" ]; then
            error "Неинтерактивный режим: нужно подтвердить, что доступ ${NEW_USER}@${SERVER_IP}:${SSH_PORT} уже проверен — задайте REMNA_CONFIRM_SSH_ACCESS=1 (иначе root не блокируется, риск потери доступа)."
            return 1
        fi
    else
        echo ""
        echo "ОСТАНОВКА: откройте НОВОЕ окно терминала и проверьте вход:"
        echo "    ssh ${NEW_USER}@${SERVER_IP} -p ${SSH_PORT}"
        echo "и что sudo работает: sudo whoami"
        echo ""
        echo "${C_YELLOW}    ВАЖНО: если вход не проходит — прежде чем считать, что sshd сломан,"
        echo "    проверьте панель управления вашего VPS-провайдера. Многие провайдеры"
        echo "    (кроме ufw внутри самой машины) дают ОТДЕЛЬНЫЙ облачный"
        echo "    firewall/Security Group — если там не разрешён порт ${SSH_PORT}/tcp,"
        echo "    вход не будет работать вообще независимо от того, что настроено"
        echo "    внутри сервера. Текущую сессию НЕ закрывайте, пока не убедитесь,"
        echo "    что попадаете новым пользователем.${C_RESET}"
        echo ""
        if ! confirm_yn "Успешно зашли новым пользователем по новому порту?" "n"; then
            error "root НЕ заблокирован. Разберитесь с доступом и запустите скрипт снова."
            return 1
        fi
    fi
    passwd -l root

    # ДОБАВЛЕНО: если для входа настроен SSH-ключ (а не только пароль) и мы
    # только что вручную подтвердили, что он реально работает — предлагаем
    # закрыть ещё одну частую дыру: вход по паролю остаётся включён по
    # умолчанию для ВСЕХ пользователей, только root заблокирован отдельно.
    # Делаем это ПОСЛЕ подтверждения входа (не раньше!) и с тем же откатом
    # при синтаксической ошибке, что и выше.
    if [ -n "${NEW_USER_SSH_KEY:-}" ]; then
        local disable_pw=false
        if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
            [ "${REMNA_DISABLE_PASSWORD_AUTH:-0}" = "1" ] && disable_pw=true
        else
            confirm_yn "SSH-ключ подтверждён. Отключить вход по паролю для ВСЕХ пользователей (только ключи)?" "n" && disable_pw=true
        fi
        if [ "$disable_pw" = true ]; then
            # Бэкап именно перед этой правкой — если sshd -t не пройдёт, откатываем
            # только её, не трогая уже подтверждённые рабочие Port/PermitRootLogin.
            local pw_backup
            pw_backup="${sshd_config}.bak.pwauth.$(date +%s)"
            cp "$sshd_config" "$pw_backup"

            sed -i -E "s/^#?PasswordAuthentication .*/PasswordAuthentication no/" "$sshd_config"
            grep -q "^PasswordAuthentication no$" "$sshd_config" || echo "PasswordAuthentication no" >> "$sshd_config"

            # Та же логика, что и с Port/PermitRootLogin выше: drop-in из
            # /etc/ssh/sshd_config.d/ может переопределить PasswordAuthentication
            # обратно в "yes" (это самая частая директива именно в cloud-init
            # drop-in'ах), сводя на нет только что сделанную правку.
            if [ -d "$dropin_dir" ]; then
                for dropin in "$dropin_dir"/*.conf; do
                    [ -f "$dropin" ] || continue
                    if grep -qE '^\s*#?\s*PasswordAuthentication\s' "$dropin"; then
                        cp "$dropin" "${dropin}.bak.pwauth.$(date +%s)"
                        sed -i -E "s/^\s*#?\s*PasswordAuthentication\s+.*/PasswordAuthentication no/" "$dropin"
                        warn "Найден и поправлен конфликтующий PasswordAuthentication в ${dropin}."
                    fi
                done
            fi

            if sshd -t -f "$sshd_config" 2>/dev/null; then
                systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null
                sleep 1
                if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
                    info "Вход по паролю отключён (PasswordAuthentication no)."
                    rm -f "$pw_backup"
                else
                    error "sshd не поднялся после отключения пароля — откатываю конфиг и перезапускаю с прежними настройками."
                    cp "$pw_backup" "$sshd_config"
                    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null
                    rm -f "$pw_backup"
                fi
            else
                warn "PasswordAuthentication no не прошло проверку sshd -t — откатываю эту правку, пароль остаётся включён."
                cp "$pw_backup" "$sshd_config"
                rm -f "$pw_backup"
            fi
        fi
    fi
}

base_step_04_firewall() {
    # Закрываем старый SSH-порт (если менялся) — иначе дыры в firewall копятся при каждом перезапуске Base.
    local old_ssh_port
    old_ssh_port=$(get_saved_ssh_port) || old_ssh_port=""

    ufw default deny incoming
    ufw default allow outgoing

    if [ -n "$old_ssh_port" ] && [ "$old_ssh_port" != "$SSH_PORT" ]; then
        ufw delete limit "${old_ssh_port}/tcp" 2>/dev/null || true
        ufw delete allow "${old_ssh_port}/tcp" 2>/dev/null || true
        info "Старое правило firewall для SSH-порта ${old_ssh_port} удалено."
    fi

    ufw limit "${SSH_PORT}/tcp"
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable

    save_ssh_port_conf "$SSH_PORT"
}

# Примечание: notify.sh (уведомления в мессенджер/ntfy) — часть отдельной
# роли Ntfy в полном remnakit. Здесь PAM-хук и fail2ban-экшен настраиваются
# как обычно, но пока notify.sh не существует, они тихо ничего не делают
# (проверка -x перед вызовом) — не ошибка, просто no-op до его появления.

base_step_05_pam_hook() {
    mkdir -p "$BIN_DIR"
    cat > "${BIN_DIR}/ssh-login-notify.sh" << EOF
#!/bin/bash
if [ "\$PAM_TYPE" = "open_session" ] && [ -x ${BIN_DIR}/notify.sh ]; then
    ${BIN_DIR}/notify.sh login_ok "\$PAM_USER" "\$PAM_RHOST"
fi
EOF
    chmod +x "${BIN_DIR}/ssh-login-notify.sh"

    grep -q "ssh-login-notify.sh" /etc/pam.d/sshd 2>/dev/null || \
        echo "session optional pam_exec.so ${BIN_DIR}/ssh-login-notify.sh" >> /etc/pam.d/sshd
}

base_step_06_fail2ban() {
    mkdir -p /etc/fail2ban/action.d
    cat > /etc/fail2ban/action.d/ntfy.conf << EOF
[Definition]
actionban = /bin/sh -c '[ -x ${BIN_DIR}/notify.sh ] && ${BIN_DIR}/notify.sh login_fail <ip> || true'
actionunban =
EOF

    cat > /etc/fail2ban/jail.local << EOF
[sshd]
enabled = true
port = ${SSH_PORT}
maxretry = 4
findtime = 600
bantime = 3600
action = %(action_)s
         ntfy
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban
}

base_step_07_unattended_upgrades() {
    cat > /etc/apt/apt.conf.d/50unattended-upgrades-custom << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
EOF
    cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    cat > /etc/apt/apt.conf.d/99-remnawave-notify << EOF
Unattended-Upgrade::Post-Invoke-Success {"${BIN_DIR}/notify.sh update_done || true";};
EOF
}

base_step_08_bbr() {
    if ! grep -qE '^\s*net\.ipv4\.tcp_congestion_control\s*=\s*bbr\s*$' /etc/sysctl.conf 2>/dev/null; then
        cat >> /etc/sysctl.conf << 'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
    fi
    sysctl -p >/dev/null 2>&1 || true
}

base_step_09_swap() {
    if [ -f /swapfile ]; then
        info "Swap уже существует, пропускаю."
        return 0
    fi
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 >/dev/null 2>&1
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q "^/swapfile" /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
}

base_step_10_timezone() {
    # Не изобретаем свой список/валидатор — используем штатный интерактивный
    # диалог выбора региона/города, тот же, что в инсталляторе Debian/Ubuntu.
    if [ -t 0 ]; then
        dpkg-reconfigure tzdata
    else
        info "Неинтерактивный запуск — таймзона не изменена (текущая: $(timedatectl show -p Timezone --value 2>/dev/null))."
    fi
}

base_step_11_cleanup_script() {
    cat > "$CLEANUP_SCRIPT" << EOF
#!/bin/bash
exec ${SCRIPT_PATH} --cleanup
EOF
    chmod +x "$CLEANUP_SCRIPT"
}

base_step_12_healthcheck_script() {
    cat > "$HEALTHCHECK_SCRIPT" << EOF
#!/bin/bash
exec ${SCRIPT_PATH} --check
EOF
    chmod +x "$HEALTHCHECK_SCRIPT"
}

base_step_13_cron() {
    local cron_tmp min hour
    cron_tmp=$(mktemp)
    crontab -l 2>/dev/null > "$cron_tmp" || true

    hour="${REBOOT_TIME%%:*}"
    min="${REBOOT_TIME##*:}"

    # Старую строку ребута удаляем безусловно — литерал не ловит смену дня/времени.
    if grep -qF "shutdown -r now" "$cron_tmp"; then
        local cron_tmp2
        cron_tmp2=$(mktemp)
        grep -vF "shutdown -r now" "$cron_tmp" > "$cron_tmp2" || true
        mv "$cron_tmp2" "$cron_tmp"
    fi
    echo "${min} ${hour} * * ${REBOOT_DAY} /sbin/shutdown -r now" >> "$cron_tmp"

    grep -qF "reboot_done" "$cron_tmp" || \
        echo "@reboot ${NOTIFY_SCRIPT} reboot_done && sleep 30 && ${HEALTHCHECK_SCRIPT}" >> "$cron_tmp"

    if ! crontab "$cron_tmp"; then
        error "crontab отклонил новое расписание (синтаксическая ошибка) — старое расписание осталось без изменений."
        rm -f "$cron_tmp"
        return 1
    fi
    rm -f "$cron_tmp"
}

base_step_14_registry() {
    registry_add base
}

base_step_15_logrotate() {
    mkdir -p "$LOG_DIR"
    cat > /etc/logrotate.d/remnawave-setup << EOF
${LOG_FILE} {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
EOF
}

base_step_16_install_steamapp() {
    # Ставим сам дашборд ОТ ИМЕНИ NEW_USER (не root), уже созданного и
    # проверенного в base_step_02/03 — но без второго ручного входа по SSH:
    # `sudo -u "$NEW_USER"`, вызванный ИЗ root-процесса, не спрашивает пароль
    # (root и так может стать кем угодно) и не требует настройки passwordless
    # sudo для NEW_USER — в отличие от вложенного sudo изнутри уже
    # переключённой сессии, это ровно один переход привилегий без всяких
    # трюков. Именно поэтому получилось объединить всё в один скрипт: пауза
    # на ручную проверку SSH в base_step_03 — единственное, что действительно
    # нельзя автоматизировать; всё, что после неё, продолжается без остановки.
    local repo_url="https://github.com/0xERR404/cheevoscope.git"
    local home_dir app_dir
    home_dir=$(getent passwd "$NEW_USER" | cut -d: -f6)
    if [ -z "$home_dir" ] || [ ! -d "$home_dir" ]; then
        error "Не удалось определить домашний каталог ${NEW_USER}."
        return 1
    fi
    app_dir="${home_dir}/cheevoscope"
    save_param APP_DIR "$app_dir"

    if [ -d "${app_dir}/.git" ]; then
        info "CheevoScope уже склонирован в ${app_dir} — обновляю."
        if ! sudo -u "$NEW_USER" -H git -C "$app_dir" pull --ff-only 2>/tmp/steamapp_git_err; then
            cat /tmp/steamapp_git_err >&2
            warn "git pull --ff-only не удался (история переписана force-push'ем?) — fetch + reset --hard."
            sudo -u "$NEW_USER" -H git -C "$app_dir" fetch origin || return 1
            local branch
            branch=$(sudo -u "$NEW_USER" -H git -C "$app_dir" symbolic-ref --short HEAD)
            sudo -u "$NEW_USER" -H git -C "$app_dir" reset --hard "origin/${branch}" || return 1
        fi
        rm -f /tmp/steamapp_git_err
    else
        sudo -u "$NEW_USER" -H git clone "$repo_url" "$app_dir" || return 1
    fi

    sudo -u "$NEW_USER" -H mkdir -p "${app_dir}/static" "${app_dir}/data" "${app_dir}/cache"

    if [ ! -d "${app_dir}/venv" ]; then
        sudo -u "$NEW_USER" -H python3 -m venv "${app_dir}/venv" || return 1
    fi
    sudo -u "$NEW_USER" -H "${app_dir}/venv/bin/pip" install --quiet --upgrade pip || return 1
    sudo -u "$NEW_USER" -H "${app_dir}/venv/bin/pip" install --quiet -r "${app_dir}/requirements.txt" || return 1

    local env_file="${app_dir}/.env"
    if [ -f "$env_file" ] && grep -q '^STEAM_API_KEY=' "$env_file" 2>/dev/null \
          && ! grep -q 'your_steam_api_key_here' "$env_file" 2>/dev/null; then
        info ".env уже настроен — использую существующий STEAM_API_KEY/STEAM_ID."
    else
        echo ""
        echo "    Понадобятся два значения:"
        echo "      STEAM_API_KEY — получить тут: https://steamcommunity.com/dev/apikey"
        echo "      STEAM_ID       — ваш SteamID64, найти тут: https://steamid.io/"
        echo ""
        local steam_key steam_id
        steam_key=$(ask_value "Введите ваш Steam API Key" validate_steam_key REMNA_STEAM_API_KEY) || return 1
        steam_id=$(ask_value "Введите ваш SteamID64 (начинается с 7656119...)" validate_steamid REMNA_STEAM_ID) || return 1
        cat > "$env_file" << EOF
STEAM_API_KEY=${steam_key}
STEAM_ID=${steam_id}
EOF
        chown "${NEW_USER}:${NEW_USER}" "$env_file"
        chmod 600 "$env_file"
    fi

    # RetroAchievements-вкладка — необязательна: без RA_USERNAME/RA_API_KEY
    # дашборд по-прежнему работает, просто вкладка RetroAchievements будет
    # показывать пустое состояние. Спрашиваем отдельным вопросом, а не
    # заставляем всех, кому нужен только Steam-раздел.
    if [ -f "$env_file" ] && grep -q '^RA_API_KEY=' "$env_file" 2>/dev/null \
          && ! grep -q 'your_ra_api_key_here' "$env_file" 2>/dev/null; then
        info ".env уже настроен — использую существующий RA_USERNAME/RA_API_KEY."
    elif confirm_yn "Настроить вкладку RetroAchievements сейчас?" n REMNA_SETUP_RA; then
        echo ""
        echo "    Понадобятся два значения:"
        echo "      RA_USERNAME — ваш логин на retroachievements.org"
        echo "      RA_API_KEY  — Settings → Keys на сайте retroachievements.org"
        echo ""
        local ra_username ra_api_key
        ra_username=$(ask_value "Введите ваш RA username" validate_ra_username REMNA_RA_USERNAME) || return 1
        ra_api_key=$(ask_value "Введите ваш RA API Key" validate_ra_api_key REMNA_RA_API_KEY) || return 1
        {
            echo "RA_USERNAME=${ra_username}"
            echo "RA_API_KEY=${ra_api_key}"
        } >> "$env_file"
        chown "${NEW_USER}:${NEW_USER}" "$env_file"
        chmod 600 "$env_file"
    else
        info "RetroAchievements пропущен — вкладка будет пустой, пока не добавите RA_USERNAME/RA_API_KEY в .env вручную и не перезапустите сервис."
    fi

    cat > /etc/systemd/system/cheevoscope.service << EOF
[Unit]
Description=CheevoScope
After=network.target

[Service]
User=${NEW_USER}
WorkingDirectory=${app_dir}
Environment="PATH=${app_dir}/venv/bin"
ExecStart=${app_dir}/venv/bin/uvicorn web:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable cheevoscope --quiet
    systemctl restart cheevoscope
    sleep 2
    if ! systemctl is-active --quiet cheevoscope; then
        error "cheevoscope не запустился. Смотрите: journalctl -u cheevoscope -n 50 --no-pager"
        return 1
    fi
    info "cheevoscope запущен и слушает 0.0.0.0:8000."

    # Почасовая автопроверка новых достижений (quick-режим, см.
    # app/hourly_refresh.py) — отдельный oneshot-сервис + таймер, а не
    # что-то внутри самого веб-процесса: так она переживает перезапуск
    # cheevoscope и видна в journalctl отдельной строкой.
    cat > /etc/systemd/system/cheevoscope-refresh.service << EOF
[Unit]
Description=CheevoScope hourly achievement refresh

[Service]
Type=oneshot
User=${NEW_USER}
WorkingDirectory=${app_dir}
Environment="PATH=${app_dir}/venv/bin"
ExecStart=${app_dir}/venv/bin/python -m app.hourly_refresh
EOF

    cat > /etc/systemd/system/cheevoscope-refresh.timer << EOF
[Unit]
Description=Run CheevoScope achievement refresh every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now cheevoscope-refresh.timer --quiet
    info "Почасовая автопроверка достижений включена (cheevoscope-refresh.timer)."
}

base_step_17_domain_https() {
    # Файрвол (80/443) уже открыт в base_step_04 — тут его трогать не нужно.
    # Полностью опционально: если отказаться, шаг всё равно считается успешным.
    local app_dir
    app_dir="$(load_state APP_DIR)"
    if [ -z "$app_dir" ]; then
        error "Не найден путь установки приложения (APP_DIR) — base_step_16 должен был его сохранить."
        return 1
    fi

    # Уже настроено предыдущим запуском — не переспрашиваем домен и пароль
    # заново при каждом повторном запуске скрипта (это же он используется и
    # для последующих обновлений кода, см. base_step_16).
    if [ -f /etc/caddy/Caddyfile ] && command -v caddy >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
        local existing_domain
        existing_domain="$(awk 'NF{print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null)"
        info "Домен и Caddy уже настроены ранее (${existing_domain}) — не перенастраиваю."
        return 0
    fi

    local want_domain=false domain=""
    if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
        if [ "${REMNA_SETUP_DOMAIN:-0}" = "1" ]; then
            want_domain=true
            domain="${REMNA_DOMAIN:-}"
            if ! validate_domain "$domain"; then
                error "REMNA_DOMAIN=\"${domain}\" не похоже на домен."
                return 1
            fi
        fi
    else
        echo ""
        read -r -p "Настроить домен и HTTPS сейчас (через Caddy)? Оставить пустым — пропустить. Домен: " domain
        if [ -n "$domain" ]; then
            while ! validate_domain "$domain"; do
                echo "    Не похоже на домен (например example.ru). Попробуйте ещё раз, или оставьте пустым чтобы пропустить."
                read -r -p "Домен: " domain
                [ -z "$domain" ] && break
            done
            [ -n "$domain" ] && want_domain=true
        fi
    fi

    if [ "$want_domain" != true ]; then
        info "Домен пропущен — дашборд доступен по http://${SERVER_IP}:8000."
        return 0
    fi

    # Перебиндиваем сервис на localhost — наружу будет торчать только Caddy.
    sed -i "s|--host 0.0.0.0|--host 127.0.0.1|" /etc/systemd/system/cheevoscope.service
    systemctl daemon-reload
    systemctl restart cheevoscope
    sleep 1

    if ! command -v caddy >/dev/null 2>&1; then
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https || return 1
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg || return 1
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            > /etc/apt/sources.list.d/caddy-stable.list || return 1
        apt-get update -qq || return 1
        apt-get install -y -qq caddy || return 1
    else
        info "Caddy уже установлен — пропускаю установку пакета."
    fi

    local auth_login auth_password
    if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
        auth_login="${REMNA_DASHBOARD_LOGIN:-}"
        auth_password="${REMNA_DASHBOARD_PASSWORD:-}"
        if [ -z "$auth_login" ] || [ ${#auth_password} -lt 8 ]; then
            error "Неинтерактивный режим: задайте REMNA_DASHBOARD_LOGIN и REMNA_DASHBOARD_PASSWORD (8+ символов)."
            return 1
        fi
    else
        echo ""
        echo "    Дашборд будет закрыт логином и паролем (HTTP Basic Auth на уровне Caddy)."
        echo ""
        auth_login=$(ask_value "Придумайте логин для входа в дашборд" validate_nonempty) || return 1
        while true; do
            read -r -s -p "Придумайте пароль (рекомендуется 12+ символов): " auth_password
            echo ""
            if [ ${#auth_password} -lt 8 ]; then
                echo "    Пароль короче 8 символов — небезопасно. Попробуйте другой."
                continue
            fi
            local auth_password_confirm
            read -r -s -p "Повторите пароль: " auth_password_confirm
            echo ""
            if [ "$auth_password" != "$auth_password_confirm" ]; then
                echo "    Пароли не совпали. Попробуйте ещё раз."
                continue
            fi
            break
        done
    fi

    local auth_hash
    auth_hash="$(caddy hash-password --plaintext "$auth_password")"
    unset auth_password

    cat > /etc/caddy/Caddyfile << EOF
${domain} {
	basicauth {
		${auth_login} ${auth_hash}
	}

	reverse_proxy 127.0.0.1:8000
}
EOF

    systemctl enable caddy --quiet
    systemctl restart caddy
    sleep 2
    if ! systemctl is-active --quiet caddy; then
        error "Caddy не запустился. journalctl -u caddy -n 50 --no-pager"
        error "Частые причины: домен ${domain} ещё не указывает на этот сервер (dig +short ${domain}),"
        error "или 80/443 не достижимы снаружи (облачный firewall провайдера, отдельно от ufw)."
        return 1
    fi

    save_param DOMAIN "$domain"
    ok "Caddy настроен: https://${domain}"
}

BASE_STEP_NAMES=(
    "Обновление системы и установка базовых пакетов"
    "Создание sudo-пользователя"
    "Смена SSH-порта и блокировка root"
    "Настройка firewall (UFW)"
    "PAM-хук уведомления о входе по SSH"
    "Fail2ban"
    "Автообновления безопасности"
    "Оптимизация сети (BBR)"
    "Swap 2GB"
    "Таймзона (dpkg-reconfigure tzdata)"
    "Скрипт cleanup.sh"
    "Скрипт healthcheck.sh"
    "Cron: плановый ребут и healthcheck после ребута"
    "Регистрация базовой настройки сервера"
    "Logrotate для setup.log"
    "Установка CheevoScope"
    "Домен и HTTPS (опционально, через Caddy)"
)
BASE_STEP_FUNCS=(
    base_step_01_update_system
    base_step_02_create_user
    base_step_03_ssh_hardening
    base_step_04_firewall
    base_step_05_pam_hook
    base_step_06_fail2ban
    base_step_07_unattended_upgrades
    base_step_08_bbr
    base_step_09_swap
    base_step_10_timezone
    base_step_11_cleanup_script
    base_step_12_healthcheck_script
    base_step_13_cron
    base_step_14_registry
    base_step_15_logrotate
    base_step_16_install_steamapp
    base_step_17_domain_https
)

install_base() {
    gather_base_inputs || return 1
    run_role_steps base BASE_STEP_NAMES BASE_STEP_FUNCS || return 1
    ok "Базовая настройка сервера и установка CheevoScope завершены."
    echo "SSH теперь на порту ${SSH_PORT}, пользователь: ${NEW_USER}, root заблокирован."
    echo ""
    local final_domain
    final_domain="$(load_state DOMAIN)"
    if [ -n "$final_domain" ]; then
        echo "Дашборд: https://${final_domain}"
    else
        echo "Дашборд: http://${SERVER_IP}:8000"
    fi
    echo ""
    echo "${C_YELLOW}Напоминание: ufw теперь разрешает 80/443/${SSH_PORT}, но если у вашего"
    echo "VPS-провайдера есть ОТДЕЛЬНЫЙ облачный firewall/Security Group в панели"
    echo "управления — проверьте те же порты и там.${C_RESET}"
    smoke_test_after_install "Базовая настройка" check_base
}

check_base() {
    local msgs="" rc=0 disk_pct mem_free_mb
    disk_pct=$(df / --output=pcent 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "$disk_pct" ] && [ "$disk_pct" -ge 85 ]; then
        msgs+="Диск заполнен на ${disk_pct}%. "
        rc=1
    fi
    mem_free_mb=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')
    if [ -n "$mem_free_mb" ] && [ "$mem_free_mb" -lt 200 ]; then
        msgs+="Свободной памяти мало: ${mem_free_mb}MB. "
        rc=1
    fi
    systemctl is-active --quiet fail2ban || { msgs+="fail2ban не активен. "; rc=1; }
    (systemctl is-active --quiet ssh || systemctl is-active --quiet sshd) || { msgs+="SSH-служба не активна. "; rc=1; }
    swapon --show --noheadings 2>/dev/null | grep -q . || { msgs+="swap не подключён. "; rc=1; }
    if systemctl list-unit-files 2>/dev/null | grep -q '^cheevoscope\.service'; then
        systemctl is-active --quiet cheevoscope || { msgs+="cheevoscope не активен. "; rc=1; }
    fi
    if systemctl list-unit-files 2>/dev/null | grep -q '^cheevoscope-refresh\.timer'; then
        systemctl is-active --quiet cheevoscope-refresh.timer || { msgs+="cheevoscope-refresh.timer не активен. "; rc=1; }
    fi
    [ -z "$msgs" ] && msgs="ok"
    echo "$msgs"
    return $rc
}

cleanup_base() {
    journalctl --vacuum-time=7d >/dev/null 2>&1 || true
    find /tmp -mindepth 1 -mtime +7 -exec rm -rf {} + 2>/dev/null || true
    apt-get clean
    apt-get autoremove -y
}

# =====================================================================
# Быстрый путь для повторных запусков: если сервис уже установлен и активен,
# сервер уже настроен раньше (незачем заново спрашивать логин/порт/домен) —
# просто обновляем код и зависимости и перезапускаем. APP_DIR/APP_USER берём
# прямо из уже существующего systemd-юнита — отдельно их нигде не храним.
# =====================================================================
update_only_fastpath() {
    local unit="/etc/systemd/system/cheevoscope.service"
    [ -f "$unit" ] || return 1
    systemctl is-active --quiet cheevoscope 2>/dev/null || return 1

    local app_dir app_user
    app_dir="$(grep '^WorkingDirectory=' "$unit" | cut -d'=' -f2-)"
    app_user="$(grep '^User=' "$unit" | cut -d'=' -f2-)"
    if [ -z "$app_dir" ] || [ -z "$app_user" ]; then
        warn "Не удалось определить APP_DIR/APP_USER из ${unit} — выполняю полную установку заново."
        return 1
    fi

    echo "==> Обнаружена рабочая установка (cheevoscope активен, ${app_dir}, пользователь ${app_user})."
    echo "==> Обновляю только код и зависимости — остальное не трогаю."
    echo ""

    if ! sudo -u "$app_user" -H git -C "$app_dir" pull --ff-only 2>/tmp/steamapp_update_err; then
        cat /tmp/steamapp_update_err >&2
        warn "git pull --ff-only не удался (история переписана force-push'ем?) — fetch + reset --hard."
        sudo -u "$app_user" -H git -C "$app_dir" fetch origin || { rm -f /tmp/steamapp_update_err; return 1; }
        local branch
        branch="$(sudo -u "$app_user" -H git -C "$app_dir" symbolic-ref --short HEAD)"
        sudo -u "$app_user" -H git -C "$app_dir" reset --hard "origin/${branch}" || { rm -f /tmp/steamapp_update_err; return 1; }
    fi
    rm -f /tmp/steamapp_update_err

    sudo -u "$app_user" -H "${app_dir}/venv/bin/pip" install --quiet --upgrade pip || return 1
    sudo -u "$app_user" -H "${app_dir}/venv/bin/pip" install --quiet -r "${app_dir}/requirements.txt" || return 1

    systemctl restart cheevoscope
    sleep 2
    if ! systemctl is-active --quiet cheevoscope; then
        error "cheevoscope не запустился после обновления. journalctl -u cheevoscope -n 50 --no-pager"
        return 1
    fi

    if [ -f /etc/caddy/Caddyfile ] && command -v caddy >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
        local existing_domain
        existing_domain="$(awk 'NF{print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null)"
        ok "Готово! Код обновлён, сервис перезапущен. Дашборд: https://${existing_domain}"
    else
        local ip
        ip="$(curl -fsS -4 -m 5 https://ifconfig.me 2>/dev/null || echo "<IP>")"
        ok "Готово! Код обновлён, сервис перезапущен. Дашборд: http://${ip}:8000"
    fi
    return 0
}

# =====================================================================
# Точка входа
# =====================================================================
main() {
    check_root
    trap 'on_error_trap $LINENO' ERR
    mkdir -p "$(dirname "$REGISTRY_FILE")" "$LOG_DIR" "$BIN_DIR"

    local arg do_check=false do_cleanup=false
    for arg in "$@"; do
        case "$arg" in
            --check)   do_check=true ;;
            --cleanup) do_cleanup=true ;;
            --debug)   DEBUG_MODE=true ;;
        esac
    done

    if [ "$do_check" = true ]; then
        check_base
        exit $?
    fi
    if [ "$do_cleanup" = true ]; then
        cleanup_base
        exit $?
    fi

    if [ "$DEBUG_MODE" = true ]; then
        echo "[DEBUG] Включена трассировка команд (--debug). Вокруг установки пароля пользователя трассировка временно отключается."
        debug_trace_on
    fi

    # --check/--cleanup (дергается cron'ом сразу после ребута) намеренно не
    # требуют интернет — сеть может подняться не сразу. Установка требует.
    check_internet || { error "Нет доступа в интернет — нужен для apt/curl. Проверьте сеть и повторите."; exit 1; }

    if update_only_fastpath; then
        exit 0
    fi

    if [ "${REMNANONINTERACTIVE:-0}" = "1" ]; then
        install_base
        exit $?
    fi

    print_banner
    if [ -f "$STATE_FILE" ]; then
        echo "Обнаружен незавершённый предыдущий запуск — будет предложено продолжить с того же места."
    fi
    install_base
}

# Позволяет source-ить файл для тестирования функций без запуска main(),
# но не ломается при запуске через "curl ... | bash" — в этом случае скрипт
# выполняется из stdin, а не из файла, BASH_SOURCE пуст, и под set -u голое
# ${BASH_SOURCE[0]} было бы обращением к необъявленной переменной. Fallback
# на $0 — стандартная переносимая идиома для этой проверки.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
    main "$@"
fi
