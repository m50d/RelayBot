from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python import log
from twisted.internet.endpoints import clientFromString
from twisted.internet.ssl import ClientContextFactory
from twisted.internet.task import LoopingCall
from twisted.application import service
from signal import signal, SIGINT
from ConfigParser import SafeConfigParser
import re, sys

#
# RelayBot is a derivative of http://code.google.com/p/relaybot/
#

log.startLogging(sys.stdout)

__version__ = "0.1"
application = service.Application("RelayBot")

def main():
    config = SafeConfigParser()
    config.read("relaybot.config")
    defaults = config.defaults()

    for section in config.sections():

        def get(option):
            if option in defaults or config.has_option(section, option):
                return config.get(section, option) or defaults[option]
            else:
                return None

        options = {}
        for option in [ "timeout", "host", "port", "nick", "channel", "info", "heartbeat", "password", "username", "realname", "ssl" ]:
            options[option] = get(option)

        mode = get("mode")

        #Not using endpoints pending http://twistedmatrix.com/trac/ticket/4735
        #(ReconnectingClientFactory equivalent for endpoints.)
        factory = None
        if mode == "Default":
            factory = RelayFactory
        elif mode == "FLIP":
            factory = FLIPFactory
        elif mode == "NickServ":
            factory = NickServFactory
            options["nickServPassword"] = get("nickServPassword")
        elif mode == "ReadOnly":
            factory = ReadOnlyFactory
            options["nickServPassword"] = get("nickServPassword")

        factory = factory(options)
        optionAsBoolean = { "": False, "false": False, "no": False, "true": True, "yes": True }
        sentinel = Object()
        ssl = options.get('ssl', sentinel)
        if(sentinel == ssl):
            raise TypeError("Cannot convert '{}' to boolean.".format(ssl))
        elif ssl:
            reactor.connectSSL(options['host'], int(options['port']), factory, ClientContextFactory(), int(options['timeout']))
        else:
            reactor.connectTCP(options['host'], int(options['port']), factory, int(options['timeout']))

    reactor.callWhenRunning(signal, SIGINT, handler)

class Communicator:
    def __init__(self):
        self.protocolInstances = {}

    def register(self, protocol):
        self.protocolInstances[protocol.identifier] = protocol

    def isRegistered(self, protocol):
        return protocol.identifier in self.protocolInstances

    def unregister(self, protocol):
        if protocol.identifier not in self.protocolInstances:
            log.msg("No protocol instance with identifier %s."%protocol.identifier)
            return
        del self.protocolInstances[protocol.identifier]

    def relay(self, protocol, message):
        for identifier in self.protocolInstances.keys():
            if identifier == protocol.identifier:
                continue
            instance = self.protocolInstances[identifier]
            instance.sayToChannel(message)

#Global scope: all protocol instances will need this.
communicator = Communicator()

class IRCRelayer(irc.IRCClient):

    def __init__(self, config):
        self.network = config['host']
        self.password = config['password']
        self.channel = config['channel']
        self.nickname = config['nick']
        self.identifier = config['identifier']
        self.privMsgResponse = config['info']
        self.heartbeatInterval = float(config['heartbeat'])
        self.username = config['username']
        self.realname = config['realname']
        log.msg("IRC Relay created. Name: %s | Host: %s | Channel: %s"%(self.nickname, self.network, self.channel))
        # IRC RFC: https://tools.ietf.org/html/rfc2812#page-4
        if len(self.nickname) > 9:
            log.msg("Nickname %s is %d characters long, which exceeds the RFC maximum of 9 characters. This may cause connection problems."%(self.nickname, len(self.nickname)))

    def formatUsername(self, username):
        return username.split("!")[0]

    def relay(self, message):
        communicator.relay(self, message)

    def signedOn(self):
        log.msg("[%s] Connected to network."%self.network)
        self.startHeartbeat()
        self.join(self.channel, "")

    def connectionLost(self, reason):
        log.msg("[%s] Connection lost, unregistering."%self.network)
        communicator.unregister(self)

    def sayToChannel(self, message):
        self.say(self.channel, message)

    def joined(self, channel):
        log.msg("Joined channel %s, registering."%channel)
        communicator.register(self)

    def privmsg(self, user, channel, message):
        #If someone addresses the bot directly, respond in the same way.
        if channel == self.nickname:
            log.msg("Recieved privmsg from %s."%user)
            self.msg(user, self.privMsgResponse)
        else:
            self.relay("[%s] %s"%(self.formatUsername(user), message))
            if message.startswith(self.nickname + ':'):
                self.say(self.channel, self.privMsgResponse)
                #For consistancy, if anyone responds to the bot's response:
                self.relay("[%s] %s"%(self.formatUsername(self.nickname), self.privMsgResponse))

    def kickedFrom(self, channel, kicker, message):
        log.msg("Kicked by %s. Message \"%s\""%(kicker, message))
        communicator.unregister(self)

    def userJoined(self, user, channel):
        self.relay("%s joined."%self.formatUsername(user))

    def userLeft(self, user, channel):
        self.relay("%s left."%self.formatUsername(user))

    def userQuit(self, user, quitMessage):
        self.relay("%s quit. (%s)"%(self.formatUsername(user), quitMessage))

    def action(self, user, channel, data):
        self.relay("* %s %s"%(self.formatUsername(user), data))

    def userRenamed(self, oldname, newname):
        self.relay("%s is now known as %s."%(self.formatUsername(oldname), self.formatUsername(newname)))


class RelayFactory(ReconnectingClientFactory):
    protocol = IRCRelayer
    #Log information which includes reconnection status.
    noisy = True

    def __init__(self, config):
        config["identifier"] = "{0}{1}{2}".format(config["host"], config["port"], config["channel"])
        self.config = config

    def buildProtocol(self, addr):
        #Connected - reset reconnect attempt delay.
        self.resetDelay()
        x = self.protocol(self.config)
        x.factory = self
        return x

class SilentJoinPart(IRCRelayer):
    def userJoined(self, user, channel):
        pass

    def userLeft(self, user, channel):
        pass

    def userQuit(self, user, quitMessage):
        pass

    def userRenamed(self, oldname, newname):
        pass

#Remove the _<numbers> that FLIP puts on the end of usernames.
class FLIPRelayer(SilentJoinPart):
    def formatUsername(self, username):
        return re.sub("_\d+$", "", IRCRelayer.formatUsername(self, username))

class FLIPFactory(RelayFactory):
    protocol = FLIPRelayer

class NickServRelayer(SilentJoinPart):
    NickServ = "nickserv"
    NickPollInterval = 30

    def signedOn(self):
        log.msg("[%s] Connected to network."%self.network)
        self.startHeartbeat()
        self.join(self.channel, "")
        self.checkDesiredNick()

    def checkDesiredNick(self):
        """
        Checks that the nick is as desired, and if not attempts to retrieve it with
        NickServ GHOST and trying again to change it after a polling interval.
        """
        if self.nickname != self.desiredNick:
            log.msg("[%s] Using GHOST to reclaim nick %s."%(self.network, self.desiredNick))
            self.msg(NickServRelayer.NickServ, "GHOST %s %s"%(self.desiredNick, self.password))
            # If NickServ does not respond try to regain nick anyway.
            self.nickPoll.start(self.NickPollInterval)

    def regainNickPoll(self):
        if self.nickname != self.desiredNick:
            log.msg("[%s] Reclaiming desired nick in polling."%(self.network))
            self.setNick(self.desiredNick)
        else:
            log.msg("[%s] Have desired nick."%(self.network))
            self.nickPoll.stop()

    def nickChanged(self, nick):
        log.msg("[%s] Nick changed from %s to %s."%(self.network, self.nickname, nick))
        self.nickname = nick
        self.checkDesiredNick()

    def noticed(self, user, channel, message):
        log.msg("[%s] Recieved notice \"%s\" from %s."%(self.network, message, user))
        #Identify with nickserv if requested
        if IRCRelayer.formatUsername(self, user).lower() == NickServRelayer.NickServ:
            msg = message.lower()
            if msg.startswith("this nickname is registered and protected"):
                log.msg("[%s] Password requested; identifying with %s."%(self.network, NickServRelayer.NickServ))
                self.msg(NickServRelayer.NickServ, "IDENTIFY %s"%self.password)
            elif msg == "ghost with your nickname has been killed." or msg == "ghost with your nick has been killed.":
                log.msg("[%s] GHOST successful, reclaiming nick %s."%(self.network,self.desiredNick))
                self.setNick(self.desiredNick)
            elif msg.endswith("isn't currently in use."):
                log.msg("[%s] GHOST not needed, reclaiming nick %s."%(self.network,self.desiredNick))
                self.setNick(self.desiredNick)

    def __init__(self, config):
        IRCRelayer.__init__(self, config)
        self.password = config['nickServPassword']
        self.desiredNick = config['nick']
        self.nickPoll = LoopingCall(self.regainNickPoll)

class ReadOnlyRelayer(NickServRelayer):
    def sayToChannel(self, message):
        pass

class ReadOnlyFactory(RelayFactory):
    protocol = ReadOnlyRelayer

class NickServFactory(RelayFactory):
    protocol = NickServRelayer

def handler(signum, frame):
    reactor.stop()

#Main if run as script, builtin for twistd.
if __name__ in ["__main__", "__builtin__"]:
        main()
