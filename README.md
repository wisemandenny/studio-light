# StudioLight

A Wi-Fi–connected recording light for Ableton Live. When you hit record, the light turns on.

## Light Setup Instructions

> Run once per studio.

1. Connect the light chip to the battery pack. Wait until it starts playing snake.
2. Once it is on, press the button on the side of the battery pack to enable **Trickle Charge Mode**. A small green dot will appear on the battery display.
3. Connect your laptop to the `Studio-Light-Setup` Wi-Fi access point (no password).
4. Open your browser and go to [http://192.168.4.1:8080](http://192.168.4.1:8080) (no login required).
5. Add your Wi-Fi information to the `Test Wi-Fi credentials` section. Test the connection. Once it works, click `Add to known networks`, then `Save & Apply`.
6. The light will display a Pokeball capture animation to indicate that it has joined the network, and then turn off. It's now ready.

## Ableton Setup Instructions

1. Download `StudioLight.zip` from the QR code or URL onto your device running Ableton.
2. Unzip `StudioLight.zip`.
3. In Ableton's left sidebar, under **Places**, click **User Library**.
4. Create a folder in your user library called `Remote Scripts` if it doesn't already exist.
5. Copy the `StudioLight` folder from the unzipped archive into your `Remote Scripts` folder (drag and drop).
6. In Ableton settings, click **Link, Tempo & MIDI**.
7. Under **MIDI**, select `StudioLight` from the **Control Surface** dropdown.
   - If `StudioLight` doesn't show up, try restarting Ableton.
   - The **Input** and **Output** columns should both say `None`.
8. When you click the record button in Ableton, the light should now turn on.

## Choosing the Light Color

By default, the recording light glows **red**. To pick a different color, change the color of your Ableton **Master track**:

1. Right-click the Master track header in the mixer.
2. Pick a color from the palette.

The recording light will switch to that color the next time you record, and will also update live if you change it while recording.

To revert to the default red, change the Master track color back to its original (or any color that matches what it was when Ableton started). The Master track color is only used as the recording light color picker because it isn't typically changed for other reasons.
