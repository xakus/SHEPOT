"""Запуск: python -m shepot (и точка входа сборки PyInstaller)."""

import multiprocessing

from shepot.app import main

if __name__ == "__main__":
    # В собранной программе дочерние процессы multiprocessing запускаются тем же
    # exe; без freeze_support они стартовали бы как второй SHEPOT.
    multiprocessing.freeze_support()
    main()
