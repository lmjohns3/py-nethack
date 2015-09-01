#!/usr/bin/env python

import collections
import logging
import numpy as np
import os
import pty
import random
import re
import select
import tempfile

import ansiterm


class CMD:
    class DIR:
        NW = 'y'
        N = 'k'
        NE = 'u'
        E = 'l'
        SE = 'n'
        S = 'j'
        SW = 'b'
        W = 'h'

        UP = '<'
        DOWN = '>'

    PICKUP = ','
    WAIT = '.'

    APPLY = 'a'
    CLOSE = 'c'
    DROP = 'd'
    EAT = 'e'
    ENGRAVE = 'E'
    FIRE = 'f'
    INVENTORY = 'i'
    OPEN = 'o'
    PAY = 'p'
    PUTON = 'P'
    QUAFF = 'q'
    QUIVER = 'Q'
    READ = 'r'
    REMOVE = 'R'
    SEARCH = 's'
    THROW = 't'
    TAKEOFF = 'T'
    WIELD = 'w'
    WEAR = 'W'
    EXCHANGE = 'x'
    ZAP = 'z'
    CAST = 'Z'

    MORE = '\x0d'      # ENTER
    KICK = '\x03'      # ^D
    TELEPORT = '\x14'  # ^T

    class SPECIAL:
        CHAT = '#chat'
        DIP = '#dip'
        FORCE = '#force'
        INVOKE = '#invoke'
        JUMP = '#jump'
        LOOT = '#loot'
        MONSTER = '#monster'
        OFFER = '#offer'
        PRAY = '#pray'
        RIDE = '#ride'
        RUB = '#rub'
        SIT = '#sit'
        TURN = '#turn'
        WIPE = '#wipe'


class InventoryItem:
    CATEGORIES = ('Amulets', 'Weapons', 'Armor', 'Comestibles',
                  'Scrolls', 'Spellbooks', 'Potions', 'Rings',
                  'Wands', 'Tools', 'Gems')

    def __init__(self, raw):
        self.raw = raw.strip()

    def __str__(self):
        return self.raw

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self)

    @property
    def is_cursed(self):
        return re.search(r'\bcursed\b', self.raw)

    @property
    def is_uncursed(self):
        return re.search(r'\buncursed\b', self.raw)

    @property
    def is_blessed(self):
        return re.search(r'\bblessed\b', self.raw)

    @property
    def is_being_worn(self):
        return re.search(r'\(being worn\)', self.raw)

    @property
    def is_in_use(self):
        return re.search(r'\((?:in use|lit)\)', self.raw)

    @property
    def duplicates(self):
        m = re.match(r'^(\d+)', self.raw)
        if not m:
            return 1
        return int(m.group(1))

    @property
    def charges(self):
        m = re.match(r'\((\d+):(\d+)\)', self.raw)
        if not m:
            return None, None
        return int(m.group(1)), int(m.group(2))

    @property
    def enchantment(self):
        m = re.match(r' ([-+]\d+) ', self.raw)
        if not m:
            return None
        return int(m.group(1))

    @property
    def named(self):
        m = re.match(r' named ([^\(]+)', self.raw)
        if not m:
            return None
        return m.group(1)


class AmuletsItem(InventoryItem):
    pass


class ArmorItem(InventoryItem):
    pass


class WeaponsItem(InventoryItem):
    @property
    def is_wielded(self):
        return re.search(r'\(weapon in hands?\)', self.raw)

    @property
    def is_alternate(self):
        return re.search(r'\(alternate weapon; not wielded\)', self.raw)

    @property
    def is_quivered(self):
        return re.search(r'\(in quiver\)', self.raw)


class ComestiblesItem(InventoryItem):
    pass


class ScrollsItem(InventoryItem):
    pass


class SpellbooksItem(InventoryItem):
    pass


class PotionsItem(InventoryItem):
    pass


class RingsItem(InventoryItem):
    pass


class WandsItem(InventoryItem):
    pass


class ToolsItem(InventoryItem):
    pass


class GemsItem(InventoryItem):
    pass


class Player:
    OPTIONS = ('CHARACTER={character}\n'
               'OPTIONS=hilite_pet,pickup_types:$?+!=/,'
               'gender:{gender},race:{race},align:{align}')

    def play(self, shape=(40, 120), **kwargs):
        self.glyphs = np.zeros(shape, int)
        self.reverse = np.zeros(shape, bool)
        self.bold = np.zeros(shape, bool)
        self._term = ansiterm.Ansiterm(*shape)
        self._need_inventory = True
        self._has_more = False
        self._command = None
        self.messages = collections.deque(maxlen=1000)
        self.stats = {}
        self.inventory = {}
        self.spells = {}

        opts = dict(character=random.choice('bar pri ran val wiz'.split()),
                    gender=random.choice('mal fem'.split()),
                    race=random.choice('elf hum'.split()),
                    align=random.choice('cha neu'.split()))
        opts.update(kwargs)

        handle = tempfile.NamedTemporaryFile()
        handle.write(self.OPTIONS.format(**opts).encode('utf-8'))
        handle.flush()

        os.environ['NETHACKOPTIONS'] = '@' + handle.name

        pty.spawn(['nethack'], self._observe, self._act)

    def choose_action(self):
        raise NotImplementedError

    def choose_answer(self):
        raise NotImplementedError

    def neighborhood(self, radius=3):
        rows, cols = self.glyphs.shape
        y, x = self.cursor
        ylo, yhi = y - radius, y + radius + 1
        xlo, xhi = x - radius, x + radius + 1
        ulo, uhi = 0, 2 * radius + 1
        vlo, vhi = 0, 2 * radius + 1
        if y < radius:
            ylo, ulo = 0, radius - y
        if x < radius:
            xlo, vlo = 0, radius - x
        if y > rows - 3 - radius:
            yhi, uhi = rows - 3, radius - (rows - y - 3)
        if x > cols - radius:
            xhi, vhi = cols, radius - (cols - x)
        hood = np.zeros((2 * radius + 1, 2 * radius + 1), np.uint8)
        hood[ulo:uhi, vlo:vhi] = self.glyphs[ylo:yhi, xlo:xhi]
        return hood

    def _parse_inventory(self, raw):
        found_inventory = False
        for category in InventoryItem.CATEGORIES:
            klass = eval('%sItem' % category)
            contents = self.inventory.setdefault(category, {})
            i = raw.find(category.encode('utf-8'))
            if i > 0:
                s = raw[i:].split(b'\x1b[7m')[0]
                for letter, name in re.findall(br' (\w) - (.*?)(?=\x1b\[)', s):
                    contents[letter.decode('utf-8')] = klass(name.decode('utf-8'))
                logging.error('inventory for %s: %s', category, contents)
                found_inventory = True
        self._need_inventory = not found_inventory

    def _parse_glyphs(self, raw):
        Y, X = self.glyphs.shape

        self._term.feed(raw)

        for y in range(Y):
            tiles = self._term.get_tiles(X * y, X * (y + 1))
            logging.debug('terminal %02d: %s', y, ''.join(str(t.glyph) for t in tiles))
            self.glyphs[y] = [ord(t.glyph) if t.glyph else 0 for t in tiles]
            self.bold[y] = [t.color['bold'] for t in tiles]
            self.reverse[y] = [t.color['reverse'] for t in tiles]

        self.cursor = (self._term.cursor['y'], self._term.cursor['x'])

        logging.info('current map:\n%s', '\n'.join(
            ''.join(chr(c) for c in r) for r in self.glyphs))
        logging.warn('current neighborhood:\n%s', '\n'.join(
            ''.join(chr(c) for c in r) for r in self.neighborhood(3)))

        self._parse_message()
        self._parse_attributes()
        self._parse_stats()

    def _parse_message(self):
        '''Parse a message from the first line on the screen.'''
        l = ''.join(chr(c) for c in self.glyphs[0])
        if l.strip() and l[0].strip():
            logging.warn('message: %s', l)
            self.messages.append(l)

    def _parse_attributes(self):
        '''Parse character attributes.'''
        l = ''.join(chr(c) for c in self.glyphs[22])
        m = re.search(r'St:(?P<st>[/\d]+)\s*'
                      r'Dx:(?P<dx>\d+)\s*'
                      r'Co:(?P<co>\d+)\s*'
                      r'In:(?P<in>\d+)\s*'
                      r'Wi:(?P<wi>\d+)\s*'
                      r'Ch:(?P<ch>\d+)\s*'
                      r'(?P<align>\S+)', l)
        if m:
            self.attributes = m.groupdict()
            logging.warn('parsed attributes: %s', ', '.join('%s: %s' % (
                k, self.attributes[k]) for k in sorted(self.attributes)))

    def _parse_stats(self):
        '''Parse stats from the penultimate line.'''
        l = ''.join(chr(c) for c in self.glyphs[23])
        m = re.search(r'Dlvl:(?P<dlvl>\S+)\s*'
                      r'\$:(?P<money>\d+)\s*'
                      r'HP:(?P<hp>\d+)\((?P<hp_max>\d+)\)\s*'
                      r'Pw:(?P<pw>\d+)\((?P<pw_max>\d+)\)\s*'
                      r'AC:(?P<ac>\d+)\s*'
                      r'Exp:(?P<exp>\d+)\s*'
                      r'(?P<hunger>Satiated|Hungry|Weak|Fainting)?\s*'
                      r'(?P<stun>Stun)?\s*'
                      r'(?P<conf>Conf)?\s*'
                      r'(?P<blind>Blind)?\s*'
                      r'(?P<burden>Burdened|Stressed|Strained|Overtaxed|Overloaded)?\s*'
                      r'(?P<hallu>Hallu)?\s*', l)
        if m:
            self.stats = m.groupdict()
            for k, v in self.stats.items():
                if v and v.isdigit():
                    self.stats[k] = int(v)
            logging.warn('parsed stats: %s', ', '.join(
                '%s: %s' % (k, self.stats[k]) for k in sorted(self.stats)))

    def _observe(self, raw):
        logging.debug('observed %d world bytes:\n%r', len(raw), raw)

        self._parse_glyphs(raw)

        if self._command is CMD.INVENTORY:
            if not self._has_more:
                self.inventory = {}
            self._parse_inventory(raw)

        self._command = None
        self._has_more = b'--More--' in raw or b'(end)' in raw

    def _act(self):
        msg = self.messages and self.messages[-1] or ''
        if self._has_more:
            self._command = CMD.MORE
        elif 'You die' in msg:
            self._command = 'q'
        elif '? ' in msg and ' written ' not in msg:
            self._command = self.choose_answer()
        elif self._need_inventory:
            self._command = CMD.INVENTORY
        else:
            self._command = self.choose_action()
        logging.warn('sending command "%s"', self._command)
        return self._command


class RandomMover(Player):
    def choose_answer(self):
        return 'n'

    def choose_action(self):
        return random.choice([
            CMD.DIR.N, CMD.DIR.NE, CMD.DIR.E, CMD.DIR.SE,
            CMD.DIR.S, CMD.DIR.SW, CMD.DIR.W, CMD.DIR.NW,
        ])


# drain all available bytes from the given file descriptor, until a complete
# timeout goes by with no new data.
def _drain(fd, timeout=0.3):
    more, _, _ = select.select([fd], [], [], timeout)
    buf = b''
    while more:
        buf += os.read(fd, 1024)
        more, _, _ = select.select([fd], [], [], timeout)
    return buf


# we almost want to do what pty.spawn does, except that we know how our child
# process works. so, we forever loop: read world state from nethack, then issue
# an action to nethack. repeat.
def _copy(fd, observe, act):
    while True:
        buf = _drain(fd)
        if buf:
            observe(buf)
            os.write(1, buf)
        pty._writen(fd, act().encode('utf-8'))


# monkeys ahoy !
pty._copy = _copy


if __name__ == '__main__':
    #import sys
    logging.basicConfig(
        stream=open('/tmp/nethack-bot.log', 'w'),
        level=logging.DEBUG,
        format='%(levelname).1s %(asctime)s %(message)s')
    rm = RandomMover()
    rm.play()
