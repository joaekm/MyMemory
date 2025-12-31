import mcp
try:
    import mcp.server
    print("mcp.server found")
    print(dir(mcp.server))
    try:
        from mcp.server.fastmcp import FastMCP
        print("FastMCP found in mcp.server.fastmcp")
    except ImportError:
        print("FastMCP NOT found in mcp.server.fastmcp")
except ImportError:
    print("mcp.server not found")





