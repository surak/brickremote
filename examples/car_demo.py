"""Drive the stock car: forward, a left turn, a right turn, then reverse."""
import asyncio

from brickremote import Car, JMCModule


async def main():
    async with JMCModule() as module:
        car = Car(module)
        for action, seconds in [(car.forward(), 2), (car.left(), 1.5), (car.right(), 1.5), (car.backward(120), 2)]:
            await action
            await asyncio.sleep(seconds)
        await car.stop()


asyncio.run(main())
