from os import set_inheritable
from socket import socket, SOL_SOCKET, SO_REUSEADDR
from asyncio import get_event_loop
from inspect import isawaitable, stack, getmodulename
from multiprocessing import Process, Event
from signal import signal, SIGTERM, SIGINT
from traceback import format_exc
from collections import deque
import logging


from sanic.config import Config
from sanic.exceptions import Handler, ServerError
from sanic.log import log
from sanic.response import HTTPResponse
from sanic.server import serve, HttpProtocol
from sanic.router import Router


class Sanic:
    def __init__(self, name=None, router=None,
                 error_handler=None, logger=None):
        if logger is None:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s: %(levelname)s: %(message)s"
            )
        if name is None:
            frame_records = stack()[1]
            name = getmodulename(frame_records[1])
        self.name = name
        self.router = router or Router()                    # 路由
        self.error_handler = error_handler or Handler(self)   # 错误处理
        self.config = Config()                                # 默认配置项
        self.loop = None
        self.debug = None
        self.sock = None
        self.processes = None
        self.request_middleware = deque()                   # 请求中间件
        self.response_middleware = deque()                  # 响应中间件
        self.blueprints = {}  # 蓝图
        self._blueprint_order = []


    # -------------------------------------------------------------------- #
    # 注册
    # -------------------------------------------------------------------- #

    # 路由装饰器
    def route(self, uri, methods=None):
        """
        使用装饰器将处理函数注册为路由
        :param uri: URL 路径
        :param methods: 允许的请求方法
        :return: 被装饰后的函数
        """
        if not uri.startswith('/'):
            uri = '/' + uri

        def response(handler):
            # 调用 Router.add 方法添加路由
            self.router.add(uri=uri, methods=methods, handler=handler)
            return handler

        return response

    # 添加路由
    def add_route(self, handler, uri, methods=None):
        """
        注册路由的非装饰器方法
        :param handler: 处理器函数
        :param uri: URL 路径
        :param methods: 允许的请求方法
        :return:
        """
        self.route(uri=uri, methods=methods)(handler)
        return handler


    # 中间件装饰器
    def middleware(self, *args, **kwargs):
        """
        使用装饰器注册中间件。格式如:
        `@app.middleware` or `@app.middleware('request')`
        """
        # 默认为 request 中间件装饰器
        attach_to = 'request'

        def register_middleware(middleware):
            if attach_to == 'request':
                self.request_middleware.append(middleware)
            if attach_to == 'response':
                self.response_middleware.appendleft(middleware)
            return middleware

        # 检查被调用方式, `@middleware` or `@middleware('AT')`
        if len(args) == 1 and len(kwargs) == 0 and callable(args[0]):
            return register_middleware(args[0])
        else:
            attach_to = args[0]
            return register_middleware

    # 异常装饰器
    def exception(self, *exceptions):
        """
        使用装饰器给异常注册处理函数。
        :param exceptions: 指定异常
        """
        def response(handler):
            for exception in exceptions:
                self.error_handler.add(exception, handler)
            return handler

        return response

    # 蓝图
    def blueprint(self, blueprint, **options):
        """
        注册蓝图到应用中
        :param blueprint: 蓝图对象
        :param options: 更改蓝图对象默认配置参数
        """
        if blueprint.name in self.blueprints:
            assert self.blueprints[blueprint.name] is blueprint, \
                'A blueprint with the name "%s" is already registered.  ' \
                'Blueprint names must be unique.' % \
                (blueprint.name,)

        else:
            self.blueprints[blueprint.name] = blueprint
            self._blueprint_order.append(blueprint)
        blueprint.register(self, options)



    # -------------------------------------------------------------------- #
    # 处理请求
    # -------------------------------------------------------------------- #

    # def converted_response_type(self, response):
    #     pass

    async def handle_request(self, request, response_callback):
        """
        从 HTTP 服务器获取请求，并发送可异步的响应对象，
        因为 HTTP 服务器只期望发送响应对象，所以需要在这里进行异常处理
        :param request: HTTP 请求对象
        :param response_callback: 可异步的 response 回调函数
        """
        try:

            response = False

            # -------------------------------------------- #
            # 请求中间件
            # -------------------------------------------- #

            # 执行请求中间件
            if self.request_middleware:
                for middleware in self.request_middleware:
                    response = middleware(request)
                    if isawaitable(response):
                        response = await response
                    if response:
                        break

            # 没有中间件
            if not response:
                # -------------------------------------------- #
                # 执行处理器
                # -------------------------------------------- #

                # 在路由中获得处理函数
                handler, args, kwargs = self.router.get(request)
                if handler is None:
                    raise ServerError(
                        ("'None' was returned while requesting a "
                         "handler from the router"))

                # Run response handler
                response = handler(request, *args, **kwargs)
                if isawaitable(response):
                    response = await response


            # -------------------------------------------- #
            # 响应中间件
            # --------------------------------------------

            if self.response_middleware:
                for middleware in self.response_middleware:
                    _response = middleware(request, response)
                    if isawaitable(_response):
                        _response = await _response
                    if _response:
                        response = _response
                        break

        except Exception as e:
            # -------------------------------------------- #
            # 生成响应失败
            # -------------------------------------------- #

            try:
                response = self.error_handler.response(request, e)  # 异常处理部分
                if isawaitable(response):
                    response = await response   # 异步返回异常
            except Exception as e:
                if self.debug:
                    response = HTTPResponse(
                        "Error while handling error: {}\nStack: {}".format(
                            e, format_exc()))
                else:
                    response = HTTPResponse(
                        "An error occured while handling an error")

        # 回调函数处理 response
        response_callback(response)

    # -------------------------------------------------------------------- #
    # 执行
    # -------------------------------------------------------------------- #

    def run(self, host="127.0.0.1", port=8000, debug=False, sock=None,
            workers=1, loop=None, protocol=HttpProtocol, backlog=100,
            stop_event=None):
        """
        运行 HTTP 服务器并一直监听，直到收到键盘终端操作或终止信号。
        在终止时，在关闭时释放所有连接。
        :param host: 服务器地址
        :param port: 服务器端口
        :param debug: 开启 debug 输出
        :param sock: 服务器接受数据的套接字
        :param workers: 进程数
        :param loop: 异步事件循环
        :param protocol: 异步协议子类
        """
        self.error_handler.debug = True
        self.debug = debug
        self.loop = loop

        # 配置 server 参数
        server_settings = {
            'protocol': protocol,
            'host': host,
            'port': port,
            'sock': sock,
            'debug': debug,
            'request_handler': self.handle_request,
            'error_handler': self.error_handler,
            'request_timeout': self.config.REQUEST_TIMEOUT,
            'request_max_size': self.config.REQUEST_MAX_SIZE,
            'loop': loop,
            'backlog': backlog
        }

        if debug:
            log.setLevel(logging.DEBUG)

        # 启动服务进程
        log.info('Goin\' Fast @ http://{}:{}'.format(host, port))

        try:
            if workers == 1:
                serve(**server_settings)    # 传入 server 参数
            else:
                log.info('Spinning up {} workers...'.format(workers))

                self.serve_multiple(server_settings, workers, stop_event)

        except Exception as e:
            log.exception(
                'Experienced exception while trying to serve')

        log.info("Server Stopped")

    def stop(self):
        """
        停止服务
        """
        if self.processes is not None:
            for process in self.processes:
                process.terminate()
            self.sock.close()
        get_event_loop().stop()


    def serve_multiple(self, server_settings, workers, stop_event=None):
        """
        同时启动多个服务器进程。一直监听直到收到键盘终端操作或终止型号。
        在终止时，在关闭时释放所有连接。
        :param server_settings: 服务配置参数
        :param workers: 进程数
        :param stop_event: 终止事件
        :return:
        """
        server_settings['reuse_port'] = True

        # Create a stop event to be triggered by a signal
        if stop_event is None:
            stop_event = Event()
        signal(SIGINT, lambda s, f: stop_event.set())
        signal(SIGTERM, lambda s, f: stop_event.set())

        self.sock = socket()
        self.sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self.sock.bind((server_settings['host'], server_settings['port']))
        set_inheritable(self.sock.fileno(), True)
        server_settings['sock'] = self.sock
        server_settings['host'] = None
        server_settings['port'] = None

        self.processes = []
        for _ in range(workers):
            process = Process(target=serve, kwargs=server_settings)
            process.daemon = True
            process.start()
            self.processes.append(process)

        for process in self.processes:
            process.join()

        # 上面的进程直到它们停止前将会阻塞
        self.stop()
