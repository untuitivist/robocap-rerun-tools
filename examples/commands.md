# Example Commands

Session 48 frame-aligned RRD with offset 40:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 40 --use-proxy
```

Session 48 time-aligned RRD:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode time --use-proxy
```

True FPS inspection:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0
```

