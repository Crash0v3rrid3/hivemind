# Hivemind demo — render with: vhs docs/demo.tape
# Produces docs/demo.gif. Run against a node that is already provisioned
# (`hivemind install` done, config pointing at your shared Drive folder).
# Requires: https://github.com/charmbracelet/vhs

Output docs/demo.gif

Set FontSize 18
Set Width 1200
Set Height 700
Set Padding 20
Set Theme "Catppuccin Mocha"

Type "# A node is a local stack you run; memory syncs through a Drive folder."
Enter
Sleep 1s

Type "hivemind status"
Enter
Sleep 3s

Type "hivemind doctor"
Enter
Sleep 3s

Type "# Point any MCP client at http://localhost:<mcp_port>/mcp and call memory_add / memory_search."
Enter
Sleep 2s

Type "hivemind logs app --tail 20"
Enter
Sleep 3s
