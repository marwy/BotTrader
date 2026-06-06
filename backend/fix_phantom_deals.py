import asyncio
import sys
sys.path.insert(0, '.')
import database

async def fix():
    import aiosqlite
    async with aiosqlite.connect(database.DB_PATH) as db:
        result = await db.execute(
            "UPDATE successful_deals SET status = ? WHERE status = ?",
            ("FAILED", "BOUGHT")
        )
        await db.commit()
        print(f"Marked {result.rowcount} phantom deals as FAILED")
    count = await database.get_active_deals_count()
    print(f"Active BOUGHT deals now: {count}")

asyncio.run(fix())
