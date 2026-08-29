#!/usr/bin/env bash
# Общий вход проверок бота. Один прогон перед каждым пушем в прод.
#
# Порядок неслучаен: сначала приборы (они умеют краснеть и доказывают это
# канарейкой), потом само-проверки модулей, потом тесты. Первый красный
# останавливает всё — «остальное вроде зелёное» не является вердиктом.
#
#   bash proverka.sh
set -u
cd "$(dirname "$0")"

BEDY=0
shag() {
  echo ""
  echo "── $1 ──────────────────────────────────────────"
  shift
  if "$@"; then :; else echo "КРАСНЫЙ: $*"; BEDY=$((BEDY+1)); fi
}

shag "Прибор: запись на встречу (двойная бронь · прошлое · пояс)" python3 proverka_vstrech.py
shag "Прибор: мёртвая воронка не вернулась, живые пути на месте"  python3 proverka_voronki.py
shag "Прибор: ключ слота дневника совпадает у бота и приложения"  python3 proverka_slota.py
shag "Само-проверка: запись на встречу" python3 vstrecha.py
shag "Само-проверка: заявка на разбор" python3 razbor.py
shag "Само-проверка: дневник"          python3 dnevnik.py
shag "Живой проход записи на встречу" python3 test_vstrecha_path.py
shag "Тесты"                            python3 -m pytest -q test_funnel.py test_e2e_path.py test_lead_policy.py test_brain.py test_bank.py test_vstrecha_path.py
shag "Импорт бота целиком"              python3 -c "import bot"

echo ""
if [ "$BEDY" -eq 0 ]; then
  echo "ВСЁ ЗЕЛЁНОЕ."
else
  echo "КРАСНЫХ ШАГОВ: $BEDY — в прод не идём."
fi
exit "$BEDY"
