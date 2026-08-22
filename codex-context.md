# Project System Rules & Context

## Tech Stack
* **Runtime**: Python 3.14
* **Web Framework**: FastAPI
* **MCP Integration**: FastMCP
* **Concurrency**: Pure Asyncio (`async`/`await` natively everywhere appropriate)

## Strict Development Rules

### 1. Code Style & Density
* **Compact Signatures**: Do not wrap function arguments across multiple lines. Keep argument lists inline and dense to maximize screen real estate.
* **No Built-in Duplication**: Never write custom utility functions if a Python built-in exists. Use `set()` instead of custom unique filters, use `any()`/`all()`, etc.
* **No Pass-Throughs**: Avoid single-line wrapper or pass-through functions. Execute logic directly inside the primary calling function.

### 2. Architecture & Data Integrity
* **Atomic Writes**: All write operations must be strictly atomic. Ensure database transactions or file writes use transactional context managers and roll back completely on failure.
* **Thin Client Blueprint**: The client layer must remain completely stateless and ultra-thin. 
* **Server-Centric Logic**: All heavy computing, business rules, and query planning must reside strictly on the server or the dedicated query planner layer.

## Implementation Blueprint Example

```python
import asyncio
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP

app = FastAPI()
mcp = FastMCP("SystemServer")

# Example of dense signature, builtins, and atomic server logic
@app.post("/data/process")
async def process_data(payload: list[str], session_id: str):
    # Use python builtins (set) directly instead of custom helper functions
    unique_items = list(set(payload))
    
    # Heavy logic and atomic transaction happens entirely on the server context
    async with db.transaction(): # Enforce strict atomicity
        try:
            result = await server_query_planner.execute(unique_items, session_id)
            return {"status": "success", "data": result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
```
