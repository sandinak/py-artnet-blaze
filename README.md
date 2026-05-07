# py-artnet-blaze

ArtNet → Pixelblaze Output Expander (POE) bridge daemon for the
Evolution Show Choir "Step" units. Replaces the Fadecandy chain
while keeping the Raspberry Pi as the configurable endpoint.

## Architecture

```
QLC+ → ArtNet UDP/6454 → [Pi] → serial @ 2 Mbaud → POE → 8× WS2812
                          ↑
                    this daemon
```

Fixed-tick output decouples input jitter from pixel refresh. A 50 FPS
tick handles the stock step layout (8 × 144 LEDs) with comfortable
headroom on a Pi 3B+; bump to 60 FPS if content demands it, or to
3 Mbaud if LED density doubles.

## Install on a Pi

```bash
# 1. Enable the real UART on GPIO14/15.
sudo tee -a /boot/config.txt <<EOF
enable_uart=1
dtoverlay=disable-bt
EOF
sudo systemctl disable hciuart
sudo reboot

# 2. Wire POE: Pi TX (pin 8, GPIO14) → POE RX. Common ground.

# 3. Deploy the code.
sudo mkdir -p /opt/artnet-blaze /etc/artnet-blaze /var/log/artnet-blaze
sudo adduser --system --group --no-create-home blaze
sudo usermod -a -G dialout blaze
sudo cp artnet_blaze.py /opt/artnet-blaze/
sudo cp config.yaml.example /etc/artnet-blaze/config.yaml

sudo python3 -m venv /opt/artnet-blaze/venv
sudo /opt/artnet-blaze/venv/bin/pip install -r requirements.txt
sudo chown -R blaze:blaze /opt/artnet-blaze /etc/artnet-blaze /var/log/artnet-blaze

# 4. Install & start the service.
sudo cp systemd/artnet-blaze.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now artnet-blaze
sudo journalctl -u artnet-blaze -f
```

For Ansible deployment, the above maps 1:1 to standard roles
(`apt`, `user`, `copy`, `pip`, `systemd` modules).

## Things to verify on step 1 before rolling to all 12 Pis

1. **POE wire format.** The `poe_frame_set_channel` and
   `poe_frame_draw_all` functions match the protocol as documented
   in the `pixelblaze_output_expander` repo. Cross-check against the
   current firmware revision you're flashing — if ElectroMage ships
   a wire-format change, adjust the struct packing accordingly.
2. **Frame timing under load.** Let it run 60+ minutes with a
   continuous 50 FPS pattern. Watch `tx_errors` and `late=` in the
   periodic stats line. Both should stay at 0.
3. **Boot-time sync.** After `systemctl start`, check that strips
   come up clean and not with garbage. If they flicker during Pi
   boot, hold POE in reset via GPIO until the daemon is ready.
4. **ArtNet universe mapping.** Confirm QLC+ universe N maps to the
   strips you expect. Easy test: set one universe to solid red in
   QLC+, verify the correct two strips light red.
5. **Blackout on exit.** `systemctl stop artnet-blaze` should turn
   strips off within ~1 second, not leave them frozen on whatever
   was last painted.
6. **Frame rate headroom at target FPS.** If planning the 144 LED/m
   density doubling, test at 3 Mbaud with the doubled pixel counts
   *before* committing to hardware.

## Extending

- **Multi-step identical config via DHCP.** The bind address in
  `config.yaml` can stay `0.0.0.0` if each Pi is on its own subnet
  and QLC+ unicasts. For broadcast ArtNet, pin the bind to each
  step's specific IP to stop cross-talk in the universe counter.
- **Per-step universe offset.** If you want identical firmware on
  all 12 Pis with step-specific universes, add a
  `universe_base: N` field to config and add it to each strip's
  universe at load time. 10 lines in `build_strips`.
- **Monitoring.** Stats lines are greppable; pipe to Prometheus
  textfile collector for `rx_frames_total`, `tx_errors_total`,
  `late_ticks_total` and you can alert on a step going offline
  during a show.
