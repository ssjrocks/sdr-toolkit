package com.ssjrocks.sdrtoolkit.core

/** RTL2832U dongles, as librtlsdr lists them (rtl-sdr.c known_devices). Keep in step with res/xml/rtlsdr_devices.xml. */
object KnownDongles {
    private val DEVICES: Map<Int, String> = mapOf(
        0x0bda2832.toInt() to "Generic RTL2832U",
        0x0bda2838.toInt() to "Generic RTL2832U OEM",
        0x04136680.toInt() to "DigitalNow Quad DVB-T PCI-E card",
        0x04136f0f.toInt() to "Leadtek WinFast DTV Dongle mini D",
        0x0458707f.toInt() to "Genius TVGo DVB-T03 USB dongle (Ver. B)",
        0x0ccd00a9.toInt() to "Terratec Cinergy T Stick Black (rev 1)",
        0x0ccd00b3.toInt() to "Terratec NOXON DAB/DAB+ USB dongle (rev 1)",
        0x0ccd00b4.toInt() to "Terratec Deutschlandradio DAB Stick",
        0x0ccd00b5.toInt() to "Terratec NOXON DAB Stick - Radio Energy",
        0x0ccd00b7.toInt() to "Terratec Media Broadcast DAB Stick",
        0x0ccd00b8.toInt() to "Terratec BR DAB Stick",
        0x0ccd00b9.toInt() to "Terratec WDR DAB Stick",
        0x0ccd00c0.toInt() to "Terratec MuellerVerlag DAB Stick",
        0x0ccd00c6.toInt() to "Terratec Fraunhofer DAB Stick",
        0x0ccd00d3.toInt() to "Terratec Cinergy T Stick RC (Rev.3)",
        0x0ccd00d7.toInt() to "Terratec T Stick PLUS",
        0x0ccd00e0.toInt() to "Terratec NOXON DAB/DAB+ USB dongle (rev 2)",
        0x15545020.toInt() to "PixelView PV-DT235U(RN)",
        0x15f40131.toInt() to "Astrometa DVB-T/DVB-T2",
        0x15f40133.toInt() to "HanfTek DAB+FM+DVB-T",
        0x185b0620.toInt() to "Compro Videomate U620F",
        0x185b0650.toInt() to "Compro Videomate U650F",
        0x185b0680.toInt() to "Compro Videomate U680F",
        0x1b80d393.toInt() to "GIGABYTE GT-U7300",
        0x1b80d394.toInt() to "DIKOM USB-DVBT HD",
        0x1b80d395.toInt() to "Peak 102569AGPK",
        0x1b80d397.toInt() to "KWorld KW-UB450-T USB DVB-T Pico TV",
        0x1b80d398.toInt() to "Zaapa ZT-MINDVBZP",
        0x1b80d39d.toInt() to "SVEON STV20 DVB-T USB & FM",
        0x1b80d3a4.toInt() to "Twintech UT-40",
        0x1b80d3a8.toInt() to "ASUS U3100MINI_PLUS_V2",
        0x1b80d3af.toInt() to "SVEON STV27 DVB-T USB & FM",
        0x1b80d3b0.toInt() to "SVEON STV21 DVB-T USB & FM",
        0x1d191101.toInt() to "Dexatek DK DVB-T Dongle (Logilink VG0002A)",
        0x1d191102.toInt() to "Dexatek DK DVB-T Dongle (MSI DigiVox mini II V3.0)",
        0x1d191103.toInt() to "Dexatek Technology Ltd. DK 5217 DVB-T Dongle",
        0x1d191104.toInt() to "MSI DigiVox Micro HD",
        0x1f4da803.toInt() to "Sweex DVB-T USB",
        0x1f4db803.toInt() to "GTek T803",
        0x1f4dc803.toInt() to "Lifeview LV5TDeluxe",
        0x1f4dd286.toInt() to "MyGica TD312",
        0x1f4dd803.toInt() to "PROlectrix DV107669",
    )

    fun name(vendorId: Int, productId: Int): String? = DEVICES[(vendorId shl 16) or productId]

    fun isRtlSdr(vendorId: Int, productId: Int) = name(vendorId, productId) != null
}
