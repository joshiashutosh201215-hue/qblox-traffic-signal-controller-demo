-- Wireshark dissector for the intersection channel message (UDP 47100, multicast 239.1.0.<channel>).
-- Install: copy to ~/.local/lib/wireshark/plugins/ (or Help > About > Folders > Personal Lua Plugins).
local p = Proto("tscchan", "Traffic signal channel message")
local SYNC = { [0] = "GNSS", [1] = "PTP", [2] = "HOLDOVER", [3] = "UNSYNC" }
local QUAL = { [0] = "OK", [1] = "STUCK_ON", [2] = "LOOP_FAULT", [3] = "SILENT", [4] = "CHATTER" }
local f = p.fields
f.schema = ProtoField.uint8("tscchan.schema", "Schema version")
f.dclass = ProtoField.uint8("tscchan.device_class", "Device class", base.DEC, { [1] = "vehicle detectors", [2] = "pedestrian" })
f.channel = ProtoField.uint8("tscchan.channel", "Channel")
f.src = ProtoField.uint8("tscchan.src", "Source intersection")
f.sync = ProtoField.uint8("tscchan.sync", "Sender clock", base.DEC, SYNC)
f.n = ProtoField.uint8("tscchan.n", "Readings")
f.seq = ProtoField.uint32("tscchan.seq", "Sequence")
f.cfg = ProtoField.uint32("tscchan.cfg", "Configuration version")
f.sample = ProtoField.uint64("tscchan.sample_ns", "Sample time (UTC ns)")
f.cycle = ProtoField.uint32("tscchan.cycle", "Cycle")
f.det = ProtoField.uint8("tscchan.det", "Detector")
f.q = ProtoField.uint8("tscchan.quality", "Quality", base.DEC, QUAL)
f.pres = ProtoField.uint8("tscchan.presence", "Presence")
f.count = ProtoField.uint8("tscchan.count", "Count (10 s)")
f.occ = ProtoField.uint16("tscchan.occ", "Occupancy (permille)")
f.gap = ProtoField.uint16("tscchan.gap", "Gap (0.1 s)")
f.mac = ProtoField.bytes("tscchan.mac", "AES-CMAC-64")

function p.dissector(buf, pinfo, tree)
  if buf:len() < 104 or buf(0, 2):string() ~= "TS" then return 0 end
  pinfo.cols.protocol = "TSC-CH"
  local t = tree:add(p, buf(0, 104))
  t:add(f.schema, buf(2, 1)); t:add(f.dclass, buf(3, 1)); t:add(f.channel, buf(4, 1)); t:add(f.src, buf(5, 1))
  t:add(f.sync, buf(6, 1)); t:add(f.n, buf(7, 1)); t:add_le(f.seq, buf(8, 4)); t:add_le(f.cfg, buf(12, 4))
  t:add_le(f.sample, buf(16, 8)); t:add_le(f.cycle, buf(24, 4))
  for i = 0, 7 do
    local o = 32 + 8 * i
    local r = t:add(buf(o, 8), string.format("D%d: %s, presence %d, count %d, occupancy %.1f %%, gap %.1f s",
      buf(o, 1):uint(), QUAL[buf(o + 1, 1):uint()] or "?", buf(o + 2, 1):uint(), buf(o + 3, 1):uint(),
      buf(o + 4, 2):le_uint() / 10, buf(o + 6, 2):le_uint() / 10))
    r:add(f.det, buf(o, 1)); r:add(f.q, buf(o + 1, 1)); r:add(f.pres, buf(o + 2, 1)); r:add(f.count, buf(o + 3, 1))
    r:add_le(f.occ, buf(o + 4, 2)); r:add_le(f.gap, buf(o + 6, 2))
  end
  t:add(f.mac, buf(96, 8))
  pinfo.cols.info = string.format("channel %d from I%d seq %d, clock %s", buf(4, 1):uint(), buf(5, 1):uint(),
    buf(8, 4):le_uint(), SYNC[buf(6, 1):uint()] or "?")
  return 104
end

DissectorTable.get("udp.port"):add(47100, p)
