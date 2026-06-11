from app.main import run

if __name__ == '__main__':
    import asyncio

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print('Bot stopped.')
