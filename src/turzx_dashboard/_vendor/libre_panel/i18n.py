# SPDX-License-Identifier: GPL-3.0-or-later
"""English messages for the extracted drivers, without the app's locale loader."""


def t(message, **values):
    return message.format(**values)
