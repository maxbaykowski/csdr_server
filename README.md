# csdr_server

`csdr_server` is a feature packed RTL SDR server that allows multiple clients to connect to an SDR dongle. Clients can either request IQ data in either signed 16 bit or complex 32 bit float formats, or request already demodulated audio! Yep, no piping bytes to gnuradio or hogging up your network bandwidth by streaming everything your antenna picks up, just listen to audio!

## Important

I thoroughly tested this program and made the design choices for it, but I was also heavily assisted by AI for most of the code. I would much rather be up front about that than mislead people by making it seem like I coded this whole thing by hand.

## Features

- Server can recover if SDR dongle gets yanked from the computer
- SDR can have a fixed center frequency, or automatic tuning can be enabled to automatically retune the SDR based on connected clients
- Change SDR settings in place so you don't have to restart the server
- Clients can request IQ data at any sample rate
- Server can also stream fully demodulated audio to clients
- Clients can request AM, WFM, stereo WFM, NFM, LSB and USB demodulation
- Support for server-side FM RDS decoding
- Support for opus encoded audio to reduce network bandwidth
- Enabling/disabling of individual audio demodulators via the server configuration file

## Install

### Prerequisites

To install this project you will need to make sure you have `pip` installed on your system. The project pulls in python dependencies automatically, but the server component has the following external dependencies:
- My fork of [csdr](https://github.com/maxbaykowski/csdr), version `0.19.3` or newer
- `librtlsdr`
- `libopus` (optional, for `opus` encoding/decoding)
- [stereodemux](https://github.com/windytan/stereodemux) (optional, for server-side stereo FM)
- [redsea](https://github.com/windytan/redsea) (optional, for server-side RDS decoding)
Refer to each project's respective GitHub README for instructions on installing them. It's a bit of a tedious process, but it's not as hard as you might think.

On most x86 Linux systems, pip will also install `pyrtlsdrlib`, which bundles
`librtlsdr`. On Linux AArch64 systems, `pyrtlsdrlib` may not be available, so
install your distro's RTL-SDR library package instead. Common package names are
`rtl-sdr`, `librtlsdr0`, or `rtl-sdr-libs`, depending on the distro.

### Install using pip

Once you have all required dependencies installed, run the following command to install the server:

```bash
pip install --user git+https://github.com/maxbaykowski/csdr_server/@main
```

The main branch will always be the latest release, which is probably what you want.

If `pip` complains, which it might if you're using an externally managed python environment, just tell it to behave:

```bash
pip install --user --break-system-packages git+https://github.com/maxbaykowski/csdr_server/@main
```

This is perfectly fine, you're not going to break the system python environment by installing this package.

## Actually running the server

First copy the example configuration from this repo somewhere:

```bash
cp config.example.json5 config.json5
```

Open it up in a text editor and make sure your RTL-SDR's index and serial numbers are correct. Then run the following command:

``` bash
csdr_server -c config.json5
```

At this point the server should be running. Unless of course you screwed up somewhere along the way when installing dependencies, in which case the server will probably fail spectacularly. Once you've fixed your newbie mistakes and have the server running, you're ready for clients to connect and start hogging your system resources.

## Running as a systemd service

Running in an interactive terminal is fine for testing, but it's best to run the server as a systemd service. To do this the server needs to be available systemwide. And no, the solution is not to install the `csdr_server` package as root.

The first thing to do is to create a python virtual environment. You'll need to have the `python3-virtualenv` package installed on your system, on Debian just install `python3-full`.
Then run the following to create a virtual environment:

```bash
sudo python3 -m venv /opt/csdr_server
```

At this point we have a virtual environment, next we install the `csdr_server` package into that environment:

```bash
sudo /opt/csdr_server/bin/python3 -m pip install git+https://github.com/maxbaykowski/csdr_server/@main
```

At this point the `csdr_server` package should be installed, but we still need to wire a systemd service up to it. However, since we don't want the server to run as the all mighty root user, we give it its own user:

```bash
sudo useradd -g plugdev csdr_server
```

Note that you will need to have udev rules granting the `plugdev` group access to RTL-SDR devices. On some distros this group is `rtlsdr`, it's not consistent across distros. 

You'll also want to copy the configuration somewhere, for example under /etc:

```bash
sudo mkdir /etc/csdr_server
sudo cp config.example.json5 /etc/csdr_server/config.json5
```

Once that's done, it's time to create the actual systemd service itself:

```bash
sudo tee /etc/systemd/system/csdr_server.service >/dev/null <<'EOF'
[Unit]
Description=csdr_server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=csdr_server
Group=csdr_server
RuntimeDirectory=csdr_server
RuntimeDirectoryMode=0700
Environment=XDG_RUNTIME_DIR=/run/csdr_server
ExecStart=/opt/csdr_server/bin/csdr_server --config /etc/csdr_server/config.json5
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
Environment=PATH=/opt/csdr_server/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF
```

Finally, reload the systemd daemon and start the newly created service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now csdr_server.service
```

If you did everything correctly, the server is now running without you having to babysit it.

## Configuring the SDR dongle

I mentioned above that you should make sure that your device index and serial number are set correctly. This is the most important step to ensuring the server works as intended. You can instruct the server to strictly use index numbers by setting `rtl_serial` to `null`, but this is not recommended. Please make sure the serial number is set, as RTL index numbers often change the second you have multiple RTL SDR dongles plugged in. To look up your RTL serial number, you can run the following command:
```bash
rtl_test -d9999
```

You'll get something like this:

```
Found 1 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 00000001

No matching devices found.
```

`rtl_test` complained that there were no matching devices found because we had to give it a fake index number for it to spit out the list of connected devices.

#### Duplicate serial numbers

As you can see, my RTL-SDR Blog V4 has an index number of `0` and a serial number of `00000001`. Let me take this opportunity to tell you, a lot of RTL-SDR dongles ship with serial number `00000001`. Please do not leave this value as `00000001`, as once you have multiple SDR's connected with duplicate serials they look almost identical to your machine. The server will also fail to start if it detects devices with duplicate serial numbers because it cannot determine which SDR is which. Luckily, the RTL-SDR suite of utilities comes with an `rtl_eeprom` tool that can be used to actually assign your SDR a unique serial number, instead of the lame `00000001`, so please, do yourself a favor and change your serial number.

#### Changing your SDR's serial number

First, make sure all other RTL-SDR dongles are unplugged, except for the dongle whose serial number you want to change. Once you're done yanking all your extra RTL-SDR dongles from your machine's USB ports, you can use `rtl_eeprom` to change the serial number. In the below example we're going to change my RTL-SDR Blog's serial number from `00000001` to `18263784`. To do this, we run the following command:

```bash
rtl_eeprom -s 18263784
```

Replace `18263784` with whatever you want the serial number to be.

We get the following output:

```
Current configuration:
__________________________________________
Vendor ID:		0x0bda
Product ID:		0x2838
Manufacturer:		RTLSDRBlog
Product:		Blog V4
Serial number:		00000001
Serial number enabled:	yes
IR endpoint enabled:	yes
Remote wakeup enabled:	no
__________________________________________

New configuration:
__________________________________________
Vendor ID:		0x0bda
Product ID:		0x2838
Manufacturer:		RTLSDRBlog
Product:		Blog V4
Serial number:		18263784
Serial number enabled:	yes
IR endpoint enabled:	yes
Remote wakeup enabled:	no
__________________________________________
Write new configuration to device [y/n]? 
```

*Important*: make sure the old and new configuration, except for the serial number, is the same! If it isn't, answer `n` to the prompt. However, in my case it is the same, so we answer `y`.

If the configuration rewrite succeeds, we get the following output:

```
Configuration successfully written.
Please replug the device for changes to take effect.
```

We must then unplug and replug the dongle to see the changes. Once that's done, we run `rtl_test -d9999` again:

```bash
rtl_test -d9999
```

We get the following output:

```
Found 1 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 18263784

No matching devices found.
```

As you can see, the serial number has been successfully changed from its default `00000001` to `18263784`, and now software can properly distinguish it from other dongles!

## Additional WFM options

The `wfm` section of the configuration file has some additional options besides just the on/off switch, I suggest you have a look at them in the example config if you haven't already. The most important setting is the region. Supported regions are `us` and `europe`. By default, the setting is set to `us`. You must make sure this is set correctly as this option will affect FM deemphasis and RDS decoding.

This server supports regular, plain old mono WFM, but it can also decode stereo and RDS data. These are controlled by the `stereo_support` and `rds_support` configuration options under the wfm section of the config. Note that you need additional dependencies for clients to use these features (see above). It is also worth noting that RDS and stereo decoding will consume resources on the server. In my testing on my Intel Core I7-1185G7, 3 clients, each on their own frequency, with stereo and RDS enabled, brought my CPU from 3% up to 12%, so don't enable this unless you know your machine can handle it. If you're running the server on your Raspberry Pi that's been sitting in a bin in your garage since 2017, it's probably best to keep stereo and RDS disabled.

## Reloading the configuration in place

This server has the ability to reload most configuration settings in place so you don't have to restart the server every time you make a change. You can do this by sending `SIGHUP` to the server process. If you're running the server in your terminal like a peasant, you must find the server PID manually:

```bash
ps -e | grep csdr_server
```

Then:

```bash
kill -HUP <pid>
```

If you're using systemd, your life becomes so much easier:

```bash
sudo systemctl reload csdr_server
```

Most RTL settings can be reloaded in place. Changes to `automatic_tuning`,
`center_frequency`, and `rtl_sample_rate` are applied immediately when connected
clients still fit inside the new capture window. If the new tuning settings
would put active clients out of band, the server keeps the current RTL tuning,
remembers the latest requested settings, and applies them once clients retune or
disconnect.

Some configuration options still require a restart if adjusted:
- Enabling/disabling audio support
- Enabling/disabling individual audio demodulators
- Enabling/disabling FM stereo support
- Enabling/disabling FM RDS support
If a setting is adjusted that requires a restart, the server will log it to stdout. If you're running the server as a systemd service you can check the log with:

```bash
sudo journalctl -u csdr_server.service
```

## Using the stock client

In addition to `csdr_server`, this package comes with a flexible client utility, `csdr_server_client`, which can be used to listen to radio stations using the audio functionality from the server, as well as print raw IQ bytes to stdout. The client is cross-platform and has been tested to work with Windows, MacOS, and Linux. To install the client on Windows or MacOS, simply install csdr_server as described in the install section. Only the client dependencies will be installed when installing to Windows or MacOS. The `csdr_server` command line utility will still be exposed, but it has OS checks so it won't run on anything other than Linux.

### A note about opus decoding

Both the server  and client use the [PyOgg](https://pypi.org/project/PyOgg/) for encoding/decoding of opus data. On Windows, `libopus` is bundled with `PyOgg`, but on MacOS and Linux it's a bit more of a pain to get working, as the opus library needs to be installed through a package manager. On Linux this is no big deal as the opus libraries are available for nearly all distros, on MacOS this is a pain point, as you'll need to install [homebrew](https://brew.sh/) to install the opus libraries. This README will *not* guide users through the installation of homebrew, you should refer to the official homebrew website for instructions. If you have `homebrew` installed, you can run the following command to install opus support:

```zsh
brew install opus
```

### Command line options

| Short form command | Long form command | Description |
| ----- | ----- | ----- |
| -a | --address  | Server IP address or hostname |
| -p | --port | Server TCP port |
| -f | --frequency | Tuned frequency in Hz, or with K/M/G suffix |
| -m | --mode | Request IQ or demodulated audio |
| -s | --sample-rate | Output sample rate for IQ mode in Sps, or with K/M/G suffix |
| -F | --format | Requested output IQ format (s16 or f32) |
| -M | --modulation | Audio modulation type (am,lsb,nfm,usb,wfm,wfm-stereo) |
| None | --stdout | Write received audio stream to stdout instead of playing |
| None | --rds | Subscribe to WFM RDS events immediately after connect |
| -l | --squelch | Audio squelch level from 0 to 100; 0 disables squelch |
| -c | --audio-codec | Audio codec (pcm or opus) |
| -b | --opus-bitrate | Opus audio bitrate in bits per second, or with K/M/G suffix |
| -B | --audio-prebuffer | Audio playback prebuffer in seconds |
| -L | --audio-latency | Requested audio device latency in seconds |
| None | --audio-device | Audio output device index or case-insensitive name |
| None | --audio-hostapi| Audio host API preference; auto uses platform defaults, except Windows uses WASAPI |

#### Examples

Listen to FRS channel 1, where all the little kids talk on their walkie-talkies:

```bash
csdr_server_client -a localhost -p 7355 -f 462.5625M -m audio -M nfm -l 55
```

The above command connects to the server on `localhost` on TCP port `7355`, sets the frequency to `462.5625 MHz`, requests demodulated audio of modulation type `nfm` (narrowband FM), with a squelch level of `55`.

Listen to the little kids, with opus enabled:

```bash
csdr_server_client -a localhost -p 7355 -f 462.5625M -m audio -M nfm -l 55 -c opus -b 32K
```

The above command connects to the server on `localhost` on TCP port `7355`, sets the frequency to `462.5625 MHz`, requests demodulated audio of modulation type `nfm` (narrowband FM), with a squelch level of `55`, opus support enabled, and an opus bitrate of `32 Kbps`.

Listen to the aviation band:

```bash
csdr_server_client -a localhost -p 7355 -f 118M -m audio -M am -l 55
```

The above command connects to the server on `localhost` on TCP port `7355`, sets the frequency to `118 MHz`, requests demodulated audio of modulation type `am` (amplitude modulation), with a squelch level of `55`.

Listen to broadcast FM:

```bash
csdr_server_client -a localhost -p 7355 -f 88.1M -m audio -M wfm
```

The above command connects to the server on `localhost` on TCP port `7355`, sets the frequency to `88.1MHz`, and requests demodulated audio of modulation type `wfm` (wideband FM).

Or to listen in stereo:

```bash
csdr_server_client -a localhost -p 7355 -f 88.1M -m audio -M wfm-stereo
```

Listen in stereo with RDS decoding enabled:

```bash
csdr_server_client -a localhost -p 7355 -f 88.1M -m audio -M wfm-stereo --rds
```

Same thing as above, except the demodulation type is `wfm-stereo` and RDS decoding by specifying `--rds`. You'll need FM stereo and RDS enabled on the server for this to work.

Record the entire broadcast AM band and save it to an IQ file:

```bash
csdr_server_client -a localhost -p 7355 -f 1120K -s 2.4M > broadcast-am-cf.raw
```

The above command connects to the server on `localhost` on TCP port `7355`, requests IQ at a frequency of `1120 KHz` and a sample rate of `2.4 Ms/s`.

By default the server sends IQ of format `float`. It is also possible to request IQ data in format `s16`, which uses half as much network bandwidth:

```bash
csdr_server_client -a localhost -p 7355 -f 1120K -s 2.4M -F s16 > broadcast-am-cf.raw
```

### Interactive control

When connected to the server, you can do things such as retuning and changing demodulation type without having to kill the client process and reopen it.

#### Interactive control options

| Command | Description |
| ----- | ----- |
| frequency | Change the current frequency you're on |
| demod | Change demodulation mode, e.g. am, wfm, wfm-stereo, etc |
| rds | Start or stop RDS decoding when in FM mode (start, stop) |
| squelch | Change squelch level (0-100) |
| bitrate | When opus is in use, change the opus bitrate |

##### Examples

```
frequency 95.7M
```

Changes the currently tuned frequency to `95.7 MHz`.

```
demod wfm
```

Changes modulation mode to `wfm` (wideband FM)

```
rds start
```

Starts RDS decoding

```
rds stop
```

Stops RDS decoding

You can also string multiple commands together by separating them with a semicolon:

```
frequency 88.1M; demod wfm
```

Changes the frequency to `88.1 MHz` and sets demodulation mode to `wfm` (wideband FM).

```
frequency 88.1M; demod wfm-stereo; rds start
```

Changes the frequency to `88.1 MHz`, sets demodulation mode to `wfm` (wideband FM), and enables RDS decoding.

```
frequency 118M; demod am; squelch 55
```

Changes the frequency to `118 Mhz`, demodulation mode to AM (amplitude modulation), and the squelch level to `55`.
