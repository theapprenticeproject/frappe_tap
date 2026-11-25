sudo systemctl daemon-reload
sudo systemctl enable --now tap-lms-feedback-consumer
sudo systemctl status tap-lms-feedback-consumer

sudo systemctl daemon-reload
sudo systemctl restart tap-lms-feedback-consumer
journalctl -u tap-lms-feedback-consumer -f

sudo systemctl stop tap-lms-feedback-consumer

journalctl -u tap-lms-feedback-consumer -n 1000 | grep -C 20 "i5lh89e8q9"