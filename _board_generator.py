import asyncio
import subprocess
import aiohttp

async def board_loop():
    while True:
        try:
            async with aiohttp.ClientSession() as client_session:
                subprocess.run(["python", "lootboards.py"])
        except Exception as e:
            print(f"Error in board generation: {e}")
        print("Board generation process completed & exited. Sleeping for 2 minutes")
        await asyncio.sleep(120)

if __name__ == "__main__":
    asyncio.run(board_loop())
