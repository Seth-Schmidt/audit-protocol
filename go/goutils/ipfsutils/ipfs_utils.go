package ipfsutils

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"time"

	shell "github.com/ipfs/go-ipfs-api"
	ma "github.com/multiformats/go-multiaddr"
	log "github.com/sirupsen/logrus"
	"github.com/swagftw/gi"
	"golang.org/x/time/rate"

	datamodel2 "audit-protocol/goutils/datamodel"
	"audit-protocol/goutils/httpclient"
	"audit-protocol/goutils/settings"
)

type IpfsClient struct {
	ipfsClient            *shell.Shell
	ipfsClientRateLimiter *rate.Limiter
}

// InitClient initializes the IPFS client.
func InitClient(url string, rateLimiter *settings.RateLimiter, timeoutSecs int) *IpfsClient {
	url = ParseMultiAddrURL(url)

	ipfsHTTPClient := httpclient.GetDefaultHTTPClient()

	log.Debug("initializing the IPFS client with IPFS Daemon URL:", url)

	client := new(IpfsClient)
	client.ipfsClient = shell.NewShellWithClient(url, ipfsHTTPClient.HTTPClient)
	timeout := time.Duration(timeoutSecs * int(time.Second))
	client.ipfsClient.SetTimeout(timeout)

	log.Debugf("setting IPFS timeout of %f seconds", timeout.Seconds())

	tps := rate.Limit(10) // 10 TPS
	burst := 10

	if rateLimiter != nil {
		burst = rateLimiter.Burst

		if rateLimiter.RequestsPerSec == -1 {
			tps = rate.Inf
			burst = 0
		} else {
			tps = rate.Limit(rateLimiter.RequestsPerSec)
		}
	}

	log.Infof("rate Limit configured for IPFS Client at %v TPS with a burst of %d", tps, burst)
	client.ipfsClientRateLimiter = rate.NewLimiter(tps, burst)

	// exit if injection fails
	if err := gi.Inject(client); err != nil {
		log.Fatalln("Failed to inject dependencies", err)
	}

	return client
}

func ParseMultiAddrURL(url string) string {
	if _, err := ma.NewMultiaddr(url); err == nil {
		url = strings.Split(url, "/")[2] + ":" + strings.Split(url, "/")[4]
	}

	return url
}

func (client *IpfsClient) UploadSnapshotToIPFS(payloadCommit *datamodel2.PayloadCommitMessage) error {
	err := client.ipfsClientRateLimiter.Wait(context.Background())
	if err != nil {
		log.WithError(err).Error("ipfs rate limiter errored")

		return err
	}

	msg, err := json.Marshal(payloadCommit.Message)
	if err != nil {
		log.WithError(err).Error("failed to marshal payload commit message")

		return err
	}

	snapshotCid, err := client.ipfsClient.Add(bytes.NewReader(msg), shell.CidVersion(1))
	if err != nil {
		log.WithError(err).Error("failed to add snapshot to ipfs")

		return err
	}

	log.WithField("snapshotCID", snapshotCid).
		WithField("epochId", payloadCommit.EpochID).
		Debug("ipfs add Successful")

	payloadCommit.SnapshotCID = snapshotCid

	return nil
}

// GetPayloadCommitMessageFromIPFS returns the payload commit message from IPFS.
func (client *IpfsClient) GetPayloadCommitMessageFromIPFS(snapshotCID string, outputPath string) error {
	err := client.ipfsClientRateLimiter.Wait(context.Background())
	if err != nil {
		log.WithError(err).Error("ipfs rate limiter errored")

		return err
	}

	err = client.ipfsClient.Get(snapshotCID, outputPath)
	if err != nil {
		log.WithError(err).Error("failed to get snapshot message from ipfs")

		return err
	}

	log.Debug("successfully fetched snapshot message from ipfs and wrote in local disk")

	return nil
}
