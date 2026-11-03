import random
import time

tick = 0
while True:
    tick += 1
    print(f"tick {tick}: processed {random.randint(1, 20)} jobs")
    time.sleep(3)
