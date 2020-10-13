import sys
import argparse
import pysam
from collections import defaultdict
import numpy as np
import tempfile
import subprocess
import shlex
import os
import shutil
from cyvcf2 import VCF, Writer


class PhaseBlock(object):
    def __init__(self, id, start, end, phase, status):
        self.id = id  # identifier of the phase block in the BAM, coordinate of the first SNV
        self.start = start  # first coordinate of a read in the phase block
        self.end = end  # last coordinate of a read in the phase block
        self.phase = phase  # List with '1' and/or '2'
        self.status = status  # biphasic, monophasic, unphased


def main():
    """
    [ ] implementation done
    [ ] test done
    """
    args = get_args()
    bam = pysam.AlignmentFile(args.bam, "rb")
    vcfs_per_chromosome = []
    tmpdmos = tempfile.mkdtemp(prefix=f"mosdepth")
    tmpdvcf = tempfile.mkdtemp(prefix=f"sniffles")

    for chrom in bam.references:  # Iterate over all chromosomes separately
        eprint(f"Working on chromosome {chrom}")
        phase_blocks = check_phase_blocks(bam, chrom)
        # Adding unphased blocks by complementing
        phase_blocks.extend(get_unphased_blocks(phase_blocks, bam.get_reference_length(chrom)))

        variant_files = defaultdict(list)

        for block in phase_blocks:
            tmpbams = make_bams(bam, chrom=chrom, phase_block=block)
            for tmpbam, phase in zip(tmpbams, block.phase):
                cov = get_coverage(tmpdmos, tmpbam, chrom, block)
                if cov >= 10:  # XXX (Evaluation needed) Do not attempt to call SVs if coverage of phased block < 10
                    tmpvcf = sniffles(tmpbam, block.status)
                    variant_files[phase].append(tmpvcf)
                os.remove(tmpbam)
        h1_vcf = concat_vcf(variant_files['1'])
        h2_vcf = concat_vcf(variant_files['2'])
        unph_vcf = concat_vcf(variant_files['u'])
        chrom_vcf = merge_haplotypes(h1_vcf, h2_vcf, unph_vcf)
        vcfs_per_chromosome.append(chrom_vcf)
    concat_vcf(vcfs_per_chromosome, output=args.vcf)
    shutil.rmtree(tmpdmos)
    shutil.rmtree(tmpdvcf)


def get_args():
    """
    [x] implementation done
    [ ] test done
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Use Sniffles on a phased bam to get phased SV calls",
        add_help=True)
    parser.add_argument("-b", "--bam",
                        help="Phased bam to perform phased SV calling on",
                        required=True)
    parser.add_argument("-v", "--vcf",
                        help="output VCF file",
                        required=True)
    return parser.parse_args()


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def check_phase_blocks(bam, chromosome):
    """
    [x] implementation done
    [ ] test done

    TODO: check if regions with >2 blocks exist
    """
    phase_dict = defaultdict(list)
    coordinate_dict = defaultdict(list)
    for read in bam.fetch(contig=chromosome):
        if read.has_tag('HP'):
            phase_dict[read.get_tag('PS')].append(read.get_tag('HP'))
            coordinate_dict[read.get_tag('PS')].extend([read.reference_start, read.reference_end])
    phase_blocks = []
    for block_identifier in phase_dict.keys():
        if '1' in phase_dict[block_identifier] and '2' in phase_dict[block_identifier]:
            phase_blocks.append(
                PhaseBlock(
                    id=block_identifier,
                    start=np.amin(coordinate_dict[block_identifier]),
                    end=np.amax(coordinate_dict[block_identifier]),
                    phase=[1, 2],
                    status='biphasic')
            )
        else:
            phase_blocks.append(
                PhaseBlock(
                    id=block_identifier,
                    start=np.amin(coordinate_dict[block_identifier]),
                    end=np.amax(coordinate_dict[block_identifier]),
                    phase=[phase_dict[block_identifier][0]],
                    status='monophasic')
            )
    return sorted(phase_blocks, key=lambda x: x.start)


def get_unphased_blocks(phase_blocks, chromosome_end_position):
    """
    [x] implementation done
    [ ] test done

    Returns intervals per chromosome where no phasing information is available.

    Parameters
    ----------
        phase_blocks : PhaseBlock[]
            List of known phase block instances.

        chromosome_end_position : Int
            Index for the end position of a chromosome

    Returns
    -------
        unphased_blocks : PhaseBlock[]
            Intervals in a chromosome where the phase is not known as list of PhaseBlock instances.
    """

    start_positions = sorted([block.start for block in phase_blocks])

    unphased_intervals_starts = [0]
    unphased_intervals_ends = []

    for start in start_positions:
        max_end_position = max([block.end for block in phase_blocks if block.start == start])

        # The end positions of a known interval are the start of an unphased region
        unphased_intervals_starts.append(max_end_position)

        # The start positions of known intervals are the end of an unphased region
        unphased_intervals_ends.append(start)

    unphased_intervals_ends.append(chromosome_end_position)

    unphased_intervals = zip(unphased_intervals_starts, unphased_intervals_ends)

    unphased_blocks = [PhaseBlock(
        id='NOID',
        start=interval[0],
        end=interval[1],
        phase=['u'],
        status='unphased'
    ) for interval in unphased_intervals if interval[0] != interval[1]]

    return sorted(unphased_blocks, key=lambda x: x.start)


def make_bams(bam, chrom, phase_block):
    """
    This function will take
    - the bam file
    - the chromosome we're working on
    - the phase block to isolate

    And produces one or two temporary bam file(s) with the reads of this locus
    if the locus is phased (phase_block.phase is not 'u'/unphased)
    then only take reads assigned to this phase


    [x] implementation done
    [ ] test done
    """
    tmp_bam_paths = []
    for phase in phase_block.phase:
        _, tmppath = tempfile.mkstemp(suffix=".bam")
        tmpbam = pysam.AlignmentFile(tmppath, mode='wb', template=bam)
        if phase == 'u':
            for read in bam.fetch(contig=chrom, start=phase_block.start, end=phase_block.end):
                tmpbam.write(read)
        else:
            for read in bam.fetch(contig=chrom, start=phase_block.start, end=phase_block.end):
                if read.has_tag('HP') and read.get_tag('HP') == phase:
                    tmpbam.write(read)
        tmp_bam_paths.append(tmppath)
    return tmp_bam_paths


def get_coverage(tmpdir, tmpbam, chrom, block):
    """
    [x] implementation done
    [ ] test done
    """
    _, tmpbed = tempfile.mkstemp(suffix=".bed")
    with open(tmpbed, 'w') as outf:
        outf.write(f"{chrom}\t{block.start}\t{block.end}\n")
    subprocess.call(shlex.split(
        f"mosdepth -n -x -b {tmpbed} {tmpdir}/{chrom}.{block.start} {tmpbam}"))
    cov = np.loadtxt(f"{tmpdir}/{chrom}.{block.start}.regions.bed.gz", usecols=3, dtype=float)
    os.remove(tmpbed)
    return cov


def sniffles(tmpdvcf, tmpbam, status):
    """
    [x] implementation done
    [ ] test done

    factor: relative coverage threshold for supporting reads. Needs to be evaluated.
    """
    handle, tmppath = tempfile.mkstemp(prefix=tmpdvcf, suffix=".vcf")
    tmpd = tempfile.mkdtemp(prefix=f"sniffles_tmp")
    # Used default values in sniffles to filter SVs based on homozygous or heterozygous allelic frequency (AF).
    # Will not attempt to remove calls based on the FILTER field in VCF, which only shows unresovled insertion length other than PASS.
    subprocess.call(shlex.split(
        f"sniffles --tmp_file {tmpd} --genotype --min_homo_af 0.8 --min_het_af 0.3 -s {s} -m {tmpbam} -v {tmppath}"))
    shutil.rmtree(tmpd)
    return tmppath


def concat_vcf(vcfs, output=tempfile.mkstemp(suffix=".vcf")[1]):
    """
    [X] implementation done
    [ ] test done
    """
    if vcfs:
        cmd = f"bcftools concat -a {' '.join(vcfs)} | bcftools sort -o {output}"
        subprocess.check_output(shlex.split(cmd))
        # remove temp vcf files
        for vcf in vcfs:
            os.remove(vcf)
        return output
    else:
        return None


def merge_haplotypes(H1, H2):
    """
    [ ] implementation done
    [ ] test done
    """
    pass
    # Also think about removing the VCFs


if __name__ == '__main__':
    main()
