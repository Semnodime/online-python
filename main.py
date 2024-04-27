#!/usr/bin/env python
"""A command line client to execute files on https://www.online-python.com/"""

import argparse
import asyncio
from enum import StrEnum, auto
import json
import logging
import os
from pathlib import Path
import signal
import sys

import websockets


class OnlinePythonClient:
    uri = 'wss://repl.online-cpp.com/socket.io/?type=script&lang=python3&EIO=3&transport=websocket'

    class State(StrEnum):
        DISCONNECTED = auto()
        CONNECTED = auto()
        INITIALIZED = auto()
        WAITING_FOR_CODE = auto()
        RUNNING = auto()
        EXPECTING_OUTPUT = auto()
        SESSION_KILLED = auto()

    class OutputType(StrEnum):
        OUTPUT = auto()
        ERROR = auto()
        INPUT = auto()

    def __init__(self, files: dict, cli_args=None):
        if cli_args is None:
            cli_args = []
        self._state = self.State.DISCONNECTED
        self.files = files
        self.websocket = None
        self.output_type = None
        self.reader = None

        self.files = files
        self.cli_args = cli_args

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        logging.info(f'State: {new_state.value!r}')
        self._state = new_state

    async def handle_next_message(self):
        await self.handle_message(await self.websocket.recv())

    async def handle_next_user_input(self):
        data = await self.reader.readline()
        data = data.decode().rstrip('\n')
        await self.send_input(data)

    async def connect(self):
        assert self.state == self.State.DISCONNECTED
        async with websockets.connect(OnlinePythonClient.uri) as websocket:
            # Close the connection when receiving SIGTERM.
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, loop.create_task, websocket.close())

            # Initialize async stdin reader
            loop = asyncio.get_event_loop()
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            self.reader = reader

            # Update client attributes
            self.websocket = websocket
            self.state = self.State.CONNECTED

            # Process messages received on the connection.
            funcs = [
                self.handle_next_message,
                self.handle_next_user_input,
            ]
            tasks = [asyncio.create_task(func()) for func in funcs]
            while True:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks = [asyncio.create_task(func()) if task.done() else task for (func, task) in zip(funcs, tasks)]

    async def send_list(self, l: list):
        message = '42' + json.dumps(l)
        logging.info(f'Sending: {l!r}')
        logging.debug(f'Sending (RAW): {message!r}')
        await self.websocket.send(message)

    async def kill_session(self):
        await self.websocket.send('41')

    async def send_files_and_run(self):
        data = ['code', [dict(code=code, file_name=file_name) for file_name, code in self.files.items()],
                ' '.join(self.cli_args), next(iter(self.files))]
        await self.send_list(data)

    async def send_input(self, message):
        data = ['message', message]
        await self.send_list(data)

    async def handle_message(self, message):
        logging.debug(f'Receiving (RAW): {message!r}')
        match self.state:
            case self.State.CONNECTED:
                assert message.startswith('0{')
                self.state = self.State.INITIALIZED

            case self.State.INITIALIZED:
                assert message == '40'
                self.state = self.State.WAITING_FOR_CODE
                await self.send_files_and_run()
                self.state = self.State.RUNNING

            case self.State.RUNNING:
                match message[:2]:
                    case '45':
                        assert message.startswith('451-')
                        data = json.loads(message[4:])
                        match data[0]:
                            case 'output':
                                self.output_type = self.OutputType.OUTPUT
                                self.state = self.State.EXPECTING_OUTPUT
                            case 'err':
                                self.output_type = self.OutputType.ERROR
                                self.state = self.State.EXPECTING_OUTPUT
                            case 'input':
                                self.output_type = self.OutputType.INPUT
                                self.state = self.State.EXPECTING_OUTPUT
                            case output_type:
                                raise NotImplementedError(output_type)
                    case '42':
                        data = json.loads(message[2:])
                        method, string, number = data
                        match method:
                            case 'exit':
                                if 'Process exited' in string:
                                    logging.info(f'Exiting: {string!r}')
                                else:
                                    logging.critical(f'Exiting: {string!r}')

                                if number is None:
                                    os._exit(number)
                                else:
                                    os._exit(int(number))
                            case _:
                                raise NotImplementedError(method)

                    case '41':
                        self.state = self.State.SESSION_KILLED

                    case message_type:
                        raise NotImplementedError(message_type)

            case self.State.EXPECTING_OUTPUT:
                match self.output_type:
                    case self.OutputType.OUTPUT:
                        file = sys.stdout
                    case self.OutputType.ERROR:
                        file = sys.stderr
                    case self.OutputType.INPUT:
                        file = None
                    case output_type:
                        raise NotImplementedError(output_type)

                if file:
                    if type(message) is bytes:
                        message = message.decode()
                    logging.info(f'{self.output_type}: {message!r}')
                    print(message, file=file)
                self.state = self.State.RUNNING

            case state:
                raise NotImplementedError(state)


def parse_arguments():
    description = 'A command line client to execute files on https://www.online-python.com/"'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--log', metavar='LOG_PATH', type=str,
                        help='Enable logging to the specified file path')
    parser.add_argument('files', metavar='file', nargs='+',
                        help='Input file(s). The first will be run if --run is not specified.')
    parser.add_argument('--run', metavar='arg', nargs='*',
                        help='Run this file with optional arguments. Treat all previous files as additional files.')

    args = parser.parse_args()

    return args


async def main():
    args = parse_arguments()

    # Configure logging
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)

    # Process parsed arguments further
    files = ((args.run and [args.run[0]]) or []) + args.files
    cli_args = args.run and args.run[1:]

    # Read file contents
    files = {Path(file_name).name: Path(file_name).read_text() for file_name in args.files}

    # Run websocket client
    client = OnlinePythonClient(files=files, cli_args=cli_args)
    await client.connect()


if __name__ == '__main__':
    asyncio.run(main())
